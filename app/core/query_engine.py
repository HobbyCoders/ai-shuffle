"""
Query engine for executing Claude queries with profiles

Based on patterns from Anvil's SessionManager:
- Keep SDK clients connected for the lifetime of a session
- Don't disconnect after every query (causes async context issues)
- Create new client with 'resume' option when resuming after app restart

This module is the main orchestrator. Related logic is split across:
- query_options.py        — option-building helpers
- query_streaming.py      — streaming/truncation utilities
- query_persistence.py    — batched DB write logic and error extraction
- query_background.py     — background (fire-and-forget) query execution
- query_session_lifecycle.py — session state inspection, interrupt, active lists

All public symbols from those modules are re-exported here for backward
compatibility.
"""

import logging
import os
import re
import time
import uuid
import asyncio
from typing import Optional, Dict, Any, AsyncGenerator, Callable, List
from datetime import datetime, timezone

from claude_agent_sdk import query, ClaudeAgentOptions
from app.core.provider.claude_adapter import ClaudeAgentAdapter
from claude_agent_sdk import (
    AssistantMessage, UserMessage, TextBlock, ThinkingBlock, ToolUseBlock,
    ToolResultBlock, ResultMessage, SystemMessage,
)
# SDK 0.1.65+ surfaces server-side tool calls (advisor, web_search, web_fetch)
# as discrete content blocks instead of dropping them silently. Guard the
# import so we stay runnable against older pinned wheels in dev.
try:
    from claude_agent_sdk import ServerToolUseBlock, ServerToolResultBlock
    _SERVER_TOOL_TYPES: tuple = (ServerToolUseBlock,)
    _SERVER_TOOL_RESULT_TYPES: tuple = (ServerToolResultBlock,)
except ImportError:  # SDK < 0.1.65
    ServerToolUseBlock = ServerToolResultBlock = None  # type: ignore[assignment]
    _SERVER_TOOL_TYPES = ()
    _SERVER_TOOL_RESULT_TYPES = ()
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
    # TaskUpdatedMessage (SDK 0.2.x) can carry a task's terminal state WITHOUT a
    # following TaskNotificationMessage — notably a TaskStop/killed background task
    # (see the SDK docstring on TaskUpdatedMessage). We must handle its terminal
    # status to clear the live "Waiting for N background task(s)…" indicator.
    # Guarded so older SDK wheels stay importable.
    from claude_agent_sdk import TaskUpdatedMessage
except ImportError:
    class TaskUpdatedMessage:  # safe stub for older SDKs (isinstance never matches a real msg)
        pass
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
from claude_agent_sdk.types import StreamEvent, PermissionResultAllow, PermissionResultDeny

from app.db import database
from app.db.async_database import run_sync
from pathlib import Path
from app.core.config import settings
from app.core.session_pool import (
    SessionState, get_session_pool, is_subprocess_alive,
    requires_reconnect, graceful_disconnect, mark_subprocess_oom_killable,
    claude_credentials_mtime, credentials_rotated, refresh_subprocess_credentials,
)
from app.core.oauth import direct_oauth
from app.core.profiles import get_profile
from app.core.sync_engine import sync_engine
from app.core.permission_handler import permission_handler, _DeferSentinel
from app.core.user_question_handler import user_question_handler

from app.core.live_session_store import live_session_store
from app.core.task_registry import task_registry
from app.core.message_resolver import message_resolver, resolve_working_dir
from app.core.system_message_filter import HIDDEN_SYSTEM_SUBTYPES

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
    resolve_note_references,
    DEFAULT_SECURITY_INSTRUCTIONS,
    get_security_instructions,
    generate_environment_details,
    build_options_from_profile,
    WorktreeUnavailableError,
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
from app.core.query_persistence import (  # noqa: F401 — re-exports
    _wrap_stderr_capture,
    _extract_connection_error,
    _persist_turn_results,
    log_captured_cli_stderr,
)
from app.core.session_context import set_current_session_id
from app.core.memory_sync import reconcile_memory, push_memory
from app.core.query_background import (  # noqa: F401 — re-exports
    _pending_background_tasks,
    _store_background_task,
    _run_background_query,
    start_background_query,
)
from app.core.query_session_lifecycle import (  # noqa: F401 — re-exports
    get_session_state,
    interrupt_session,
    stop_session_task,
    stop_all_background_tasks,
    get_active_sessions,
    get_streaming_sessions,
)

logger = logging.getLogger(__name__)

# Gate for verbose SDK-frame-ordering tracing in the streaming loop. Off unless
# either env var is truthy. Read once at import so the hot path only does a bool
# check. (Mirrors the STREAM_DEBUG/AIHUB_DEBUG_STREAM gate used elsewhere.)
_STREAM_DEBUG = bool(
    os.environ.get("STREAM_DEBUG") or os.environ.get("AIHUB_DEBUG_STREAM")
)


def _rate_limit_event_dict(rli: Any, session_id: str) -> Dict[str, Any]:
    """Build the ``rate_limit`` WS frame from an SDK ``RateLimitInfo``.

    Shared by the interactive (``stream_query``) and offloaded
    (``stream_to_websocket``) streaming paths so both stamp the identical fields,
    including this install's ``instance_id`` (the frontend attributes the reading
    to the right per-account window). Cross-install propagation does NOT ride
    this frame — it happens via the shared ``usage_snapshots`` row and the
    cluster ``usage_snapshot`` broadcast (see the paired
    ``apply_live_event_and_broadcast`` call at each site).
    """
    return {
        "type": "rate_limit",
        "status": rli.status,
        "rate_limit_type": rli.rate_limit_type,
        "resets_at": rli.resets_at,
        "utilization": rli.utilization,
        # SDK 0.1.81+ overage fields (None when absent)
        "overage_status": getattr(rli, "overage_status", None),
        "overage_resets_at": getattr(rli, "overage_resets_at", None),
        "overage_disabled_reason": getattr(rli, "overage_disabled_reason", None),
        "session_id": session_id,
        "instance_id": settings.effective_instance_id,
    }


async def _apply_rate_limit_live_event(rli: Any) -> None:
    """Persist a live ``rate_limit`` refinement into the shared usage snapshot.

    A yielded ``rate_limit`` frame reaches only clients connected directly to
    THIS install's WS; peers (and offloaded/cloud-relayed views, which read the
    owning install's ``/live`` via ``live_session_store`` — where ``rate_limit``
    is never written) see the change only through the shared ``usage_snapshots``
    row persisted here plus the cluster ``usage_snapshot`` broadcast. Best-effort
    — never break streaming.
    """
    try:
        from app.core.usage_limits import usage_limits_monitor
        await usage_limits_monitor.apply_live_event_and_broadcast(
            rli.rate_limit_type, rli.utilization, rli.resets_at,
        )
    except Exception as _usage_err:  # noqa: BLE001 — never break the turn
        logger.debug(f"usage monitor live-event update skipped: {_usage_err}")


# Map the SDK's terminal TaskNotificationStatus ("completed"/"failed"/"stopped")
# onto the registry's terminal statuses. TaskNotificationMessage is ALWAYS
# terminal (the Literal has no in-progress value), so any notification completes
# the task. Unknown values fall back to "completed".
_TASK_NOTIFICATION_STATUS_MAP = {
    "completed": "completed",
    "failed": "error",
    "stopped": "killed",
}

# UNLIKE TaskNotificationMessage, a TaskUpdatedMessage is NOT always terminal — it
# fires on every state transition (pending/running/paused/completed/failed/killed,
# per the SDK's TaskUpdatedStatus literal). We act ONLY on its terminal statuses,
# and map them onto the registry vocabulary. This is a DEDICATED set (not the
# shared ``_TERMINAL_TASK_STATUSES`` in chat_ws_handler, whose values are the
# *notification* literal) so adding the update-only "killed" value here cannot
# alter the notification path's behaviour. "stopped" is included defensively even
# though TaskUpdatedStatus uses "killed" for the TaskStop case.
_TASK_UPDATE_TERMINAL_STATUS_MAP = {
    "completed": "completed",
    "failed": "error",
    "killed": "killed",
    "stopped": "killed",
}


def _task_update_terminal_status(message: Any) -> Optional[str]:
    """Return the terminal status string from a ``TaskUpdatedMessage``, or ``None``.

    Reads the status from the top-level ``.status`` attribute first, falling back
    to ``patch["status"]`` (the SDK populates the changed fields in ``patch`` and
    *may* also surface them on top-level attrs). Normalises an Enum to its value.
    Returns ``None`` for non-terminal updates (pending/running/paused) so the
    caller leaves a still-running task untouched.
    """
    raw = getattr(message, "status", None)
    if raw is None:
        patch = getattr(message, "patch", None)
        if isinstance(patch, dict):
            raw = patch.get("status")
    if raw is None:
        return None
    raw = raw.value if hasattr(raw, "value") else str(raw)
    return raw if raw in _TASK_UPDATE_TERMINAL_STATUS_MAP else None


def _record_task_message(session_id: Optional[str], message: Any) -> None:
    """Feed an SDK task message into the in-memory ``task_registry``.

    Best-effort and fully defensive: any failure is logged at debug and
    swallowed so a registry hiccup can never disturb the stream. Does NOT change
    what is yielded to the client — purely a side-effect for TaskList / /live.
    """
    if not session_id:
        return
    try:
        if isinstance(message, TaskStartedMessage):
            # The SDK emits the SAME TaskStartedMessage for every flavour of
            # task and discriminates them only via ``task_type``:
            #   "local_bash"                      → a shell command
            #   "local_agent"/"remote_agent"/
            #   "local_workflow"                  → a sub-agent / workflow
            # Map "local_bash" onto the registry's "shell" kind so it renders the
            # SHELL badge (not the agent badge). Anything else — including a missing
            # task_type from an older CLI — stays "agent", the historical behaviour,
            # right for sub-agents.
            #
            # CRITICAL: the CLI emits a local_bash TaskStartedMessage for FOREGROUND
            # shell commands too, not just run_in_background ones. Only a shell whose
            # launch tool_use the parent issued with run_in_background=true (recorded
            # via mark_background_launch when the tool_use block streamed — the
            # TaskStarted always arrives after it) is a genuine BACKGROUND shell:
            #   - it DOES park its parent turn: the parent's receive_response()
            #     returns at the parent ResultMessage while the shell keeps running,
            #     and its completion arrives LATER as a terminal
            #     TaskNotificationMessage on the SAME pooled SDK client — exactly
            #     like a background sub-agent. mark_sdk_background_task pins the
            #     parked session in the pool (else idle eviction kills the pooled
            #     client and drops the completion → frontend never unparks) and
            #     gates the between-turns drain pump via get_active_background_work.
            #     SDK shells are tracked SEPARATELY from the app's own Monitor-tool
            #     shells (mcp/builtin_tools.py), which are app-managed, independent
            #     of the pooled client, and must NOT pin.
            # A FOREGROUND shell must arm NEITHER mark: its result returns inline as
            # a normal tool_result this turn, so treating it as a background launch
            # made its at-rest notification pass the unpark gates and fired a stale
            # "<task-notification>" follow-up turn after the turn ended, re-telling
            # the model about commands it already ran and reported (2026-07-02
            # incident). It still gets a registry entry above for the TaskList/live
            # SHELL badge; complete_task below clears it when the command finishes.
            task_type = getattr(message, "task_type", None)
            kind = "shell" if task_type == "local_bash" else "agent"
            task_registry.start_task(
                session_id, message.task_id, kind, getattr(message, "description", None),
                tool_use_id=getattr(message, "tool_use_id", None),
            )
            if kind == "shell":
                tuid = getattr(message, "tool_use_id", None)
                if tuid and task_registry.was_background_launch(session_id, tuid):
                    task_registry.mark_sdk_background_task(session_id, message.task_id)
                elif tuid and task_registry.was_subagent_background_launch(session_id, tuid):
                    # SUBAGENT-lane backgrounded shell (a grandchild): arm the
                    # dedicated unpark marker ONLY — no pool pin (the launching
                    # sub-agent's own kind=="agent" record pins), no chip change.
                    # Without this the grandchild's terminal notification failed
                    # both pump gates → suppress-foreground → a sub-agent parked
                    # awaiting it stranded forever (2026-07-10 incident).
                    task_registry.mark_subagent_background_task(session_id, message.task_id)
            else:
                # Belt-and-braces for genuine BACKGROUND sub-agents. The CLI
                # (2.1.195+) launches Agent/Task subagents ASYNC without
                # run_in_background in the tool input — the launch tool_result is
                # only an ACK — so the launch gate is armed from that ACK sniff
                # (see the ToolResult branches below). If a launch WAS recorded
                # for this agent's launch tool_use_id, pin the parked session in
                # the pool the same way a background shell does; otherwise the
                # idle sweeper can evict the pooled client and drop the eventual
                # completion → the frontend never unparks. Guarded strictly on a
                # RECORDED launch so a synchronous sub-agent (result returns
                # inline this turn, never armed) is NOT resurrected as background
                # work (that reintroduced the #470 stale-follow-up-turn bug).
                tuid = getattr(message, "tool_use_id", None)
                if tuid and task_registry.was_background_launch(session_id, tuid):
                    task_registry.mark_sdk_background_task(session_id, message.task_id)
                elif tuid and task_registry.was_subagent_background_launch(session_id, tuid):
                    # Subagent-lane backgrounded grandchild agent — same
                    # unpark-only marker as the shell branch above.
                    task_registry.mark_subagent_background_task(session_id, message.task_id)
            # Scope the in-stream unpark stash to THIS turn: record that this
            # background task's TaskStartedMessage arrived during the turn now
            # streaming. Only a task in this set may fire an in-stream unpark when
            # its terminal notification lands (the genuine FAST path). A terminal
            # notification for a task NOT started this turn is a prior-turn orphan
            # leaking from the SDK's pooled-client buffer and must be suppressed.
            task_registry.mark_started_this_turn(session_id, message.task_id)
        elif isinstance(message, TaskProgressMessage):
            # Progress carries the latest description — surface it as the title.
            task_registry.update_task(
                session_id, message.task_id, title=getattr(message, "description", None)
            )
        elif isinstance(message, TaskNotificationMessage):
            raw_status = message.status
            raw_status = raw_status.value if hasattr(raw_status, "value") else str(raw_status)
            mapped = _TASK_NOTIFICATION_STATUS_MAP.get(raw_status, "completed")
            # Preserve the human-readable summary on the record before flipping
            # to terminal, then complete. (TaskNotification is always terminal.)
            summary = getattr(message, "summary", None)
            if summary is not None:
                task_registry.update_task(session_id, message.task_id, summary=summary)
            task_registry.complete_task(session_id, message.task_id, mapped)
        elif isinstance(message, TaskUpdatedMessage):
            # THE GAP this fix closes: a killed/stopped (and sometimes completed)
            # background task can report its terminal state HERE with NO following
            # TaskNotificationMessage, which would otherwise strand the registry
            # task "running" forever (frozen "Waiting for N background task(s)…"
            # indicator). Only terminal updates flip it; non-terminal ones
            # (pending/running/paused) are intentionally ignored so we never
            # clobber a still-running task. complete_task is idempotent, so if the
            # normal path later delivers a TaskNotificationMessage for the same
            # task, completing again is a harmless no-op (no error, no dup event).
            mapped = _TASK_UPDATE_TERMINAL_STATUS_MAP.get(
                _task_update_terminal_status(message) or "", None
            )
            if mapped is not None:
                task_registry.complete_task(session_id, message.task_id, mapped)
    except Exception:
        logger.debug("task_registry update for SDK task message failed", exc_info=True)


# CLI-NATIVE tools that ALWAYS spawn a background task despite carrying NO
# run_in_background/runInBackground flag in their input. The builtin `Monitor`
# tool's input is {command, description, timeout_ms, persistent} — the flag
# sniffs can never classify it, so a Monitor armed from a sub-agent sidechain
# was invisible to every launch gate. Live 2026-07-10 (this branch's debug
# instance, session 07df5136 task b5qiqqf1h): the sidechain Monitor's terminal
# notification hit the pump unarmed → decision=suppress-foreground → the
# Monitor-parked sub-agent stranded forever. Queue-op evidence shows the CLI
# enqueued the <task-notification> into the parked parent's queue and never
# self-dequeued a SUBAGENT-owned one — unlike TOP-LEVEL Monitors, whose wakes
# the CLI owns end-to-end (enqueue + self-dequeue) and which therefore must
# keep being suppressed by the app. So this name sniff applies to SIDECHAIN
# tool_uses only (both recorders below already gate on parent_tool_use_id).
_IMPLICIT_BACKGROUND_TOOL_NAMES = ("Monitor",)


def _record_subagent_lane_launches(session_id: Optional[str], message: Any) -> None:
    """Record SUBAGENT-lane run_in_background launches that arrive BETWEEN turns.

    In-stream, the ToolUseBlock branch of ``stream_to_websocket`` records these
    (``mark_subagent_background_launch``); but a backgrounded sub-agent keeps
    running after the parent parks, and its inner tool_use blocks then arrive on
    the pooled client with only the drain pump reading them. The pump calls this
    per message (right before ``_record_task_message``, so the launch mark is
    always recorded before the task's TaskStarted is processed). Sidechain
    messages only (``parent_tool_use_id`` set) — a top-level between-turns
    launch belongs to ``_cli_turn_events_from_message``. Side-effect-only and
    fully defensive, like ``_record_task_message``.
    """
    if not session_id or not getattr(message, "parent_tool_use_id", None):
        return
    try:
        if not isinstance(message, AssistantMessage):
            return
        content = message.content or []
        if isinstance(content, str):
            return
        for block in content:
            if not isinstance(block, ToolUseBlock):
                continue
            tool_id = getattr(block, "id", None)
            tool_input = block.input or {}
            _flagged = bool(
                tool_input.get("run_in_background")
                or tool_input.get("runInBackground")
            )
            # Flag-less always-background builtins (Monitor): the 2026-07-10
            # b5qiqqf1h miss — see _IMPLICIT_BACKGROUND_TOOL_NAMES.
            _implicit = getattr(block, "name", None) in _IMPLICIT_BACKGROUND_TOOL_NAMES
            if tool_id and (_flagged or _implicit):
                task_registry.mark_subagent_background_launch(session_id, tool_id)
                logger.info(
                    "task-launch-classification session=%s tool_id=%s "
                    "classification=%s (pump)",
                    session_id, tool_id,
                    "background-flag-subagent-lane" if _flagged
                    else "background-builtin-subagent-lane",
                )
    except Exception:
        logger.debug("subagent-lane launch recording failed", exc_info=True)


# Phrases that identify a backgrounded sub-agent's launch-ACK ToolResult — the
# immediate "Async agent launched successfully … working in the background …"
# placeholder the SDK returns the moment a Task is started with
# ``run_in_background``. This is a fallback to the ``run_in_background`` flag (the
# primary signal) for the rare case the flag is absent; mirrors the content sniff
# in ``live_session_store._is_background_ack`` / ``jsonl_parser``.
_SUBAGENT_LAUNCH_ACK_PHRASES = (
    "launched successfully",
    "working in the background",
    "running in the background",
    "background with id",
    "notified automatically",
)


def _looks_like_subagent_launch_ack(output: Any) -> bool:
    """Best-effort sniff: does this Task ToolResult read like a background launch-ACK?"""
    text = output if isinstance(output, str) else str(output or "")
    low = text.lower()
    return any(phrase in low for phrase in _SUBAGENT_LAUNCH_ACK_PHRASES)


def _subagent_events_from_message(message: Any) -> List[Dict[str, Any]]:
    """Convert a drained sub-agent inner message into ``subagent_*`` events.

    A SLOW background sub-agent's inner activity (assistant text + tool calls)
    arrives on the pooled SDK client AFTER the parent turn's ``receive_response``
    has already returned, so the in-stream conversion in ``stream_to_websocket``
    never sees it. The between-turns drain pump calls this to convert those
    messages (identified by ``parent_tool_use_id``) into the SAME events the
    in-stream path yields, so the subagent inner group fills in instead of
    rendering "no activity recorded". Returns ``[]`` for anything that is not a
    sub-agent inner message.
    """
    parent_tool_id = getattr(message, "parent_tool_use_id", None)
    if not parent_tool_id:
        return []
    content = getattr(message, "content", None) or []
    if isinstance(content, str):
        return []

    events: List[Dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            if block.text:
                events.append({
                    "type": "subagent_chunk",
                    "agent_id": parent_tool_id,
                    "content": block.text,
                })
        elif isinstance(block, ToolUseBlock):
            events.append({
                "type": "subagent_tool_use",
                "agent_id": parent_tool_id,
                "name": block.name,
                "id": getattr(block, "id", None),
                "input": block.input or {},
            })
        elif isinstance(block, ToolResultBlock):
            events.append({
                "type": "subagent_tool_result",
                "agent_id": parent_tool_id,
                "name": "unknown",
                "tool_use_id": getattr(block, "tool_use_id", None),
                "output": truncate_large_payload(block.content) if block.content else "",
                "is_error": getattr(block, "is_error", False),
            })
    return events


def _cli_turn_events_from_message(
    session_id: str,
    message: Any,
    tool_id_to_name: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Convert a TOP-LEVEL message of a CLI-driven (autonomous) turn into the
    SAME main-lane events the in-stream loop yields.

    THE AT-REST WAKE CLASS (round 4 of the duplicate-notification fix): when a
    top-level backgrounded Task/Agent comes to rest while the parent is PARKED
    (between turns), the CLI itself starts a follow-up turn — it injects a
    ``[SYSTEM NOTIFICATION - NOT USER INPUT] … <task-notification>`` user-lane
    string and the model answers it. Those messages arrive on the pooled client
    with only the between-turns drain pump reading them; without this converter
    the assistant's answer reached ONLY the durable SDK transcript mirror (so it
    appeared after a page refresh) but never the live store — an actively-watched
    session showed nothing until reload. The pump dispatches these events through
    ``dispatch_event`` (the identical handler chain a normal turn uses), so the
    live store appends + ``update_id`` bumps and park-pollers render the CLI's
    own wake turn live.

    Parity rules (mirrors ``stream_to_websocket`` + the reload parser):
    - The injected user-lane STRING (and any top-level user TextBlock) yields NO
      event — in-stream these steering messages are never rendered, and the
      reload path (``jsonl_parser._is_task_notification``) hides them too.
    - Assistant ``TextBlock`` → ``chunk`` (same event the non-partial in-stream
      path yields); ``ThinkingBlock`` → a ``thinking_block`` marker the pump
      routes to ``live_session_store.append_thinking``/``finalize_thinking``
      (there is no dispatchable thinking event type).
    - ``ToolUseBlock`` named Task/Agent → ``subagent_start`` (same shape as
      in-stream) so a sub-agent launched BY the CLI-driven turn gets its node;
      any other tool → ``tool_use``. Launch classification side-effects mirror
      in-stream: a ``run_in_background`` input arms ``mark_background_launch``,
      and a Task/Agent ToolResult that reads like the async launch-ACK arms it
      too (``_looks_like_subagent_launch_ack``) — otherwise a wake for an agent
      chained from a CLI-driven turn would be misclassified as foreground.
    - User ``ToolResultBlock`` → ``tool_result`` (names resolved through the
      shared ``tool_id_to_name`` map the caller threads across messages).

    Returns ``[]`` for sub-agent inner messages (``parent_tool_use_id`` set —
    those belong to ``_subagent_events_from_message``) and for any non
    Assistant/User message type.
    """
    if getattr(message, "parent_tool_use_id", None):
        return []

    events: List[Dict[str, Any]] = []

    if isinstance(message, AssistantMessage):
        for block in message.content or []:
            if isinstance(block, TextBlock):
                if block.text and block.text.strip():
                    events.append({"type": "chunk", "content": block.text})
            elif isinstance(block, ThinkingBlock):
                if block.thinking:
                    events.append({
                        "type": "thinking_block",
                        "content": block.thinking,
                    })
            elif isinstance(block, ToolUseBlock):
                tool_id = getattr(block, "id", None)
                tool_input = block.input or {}
                if tool_id:
                    tool_id_to_name[tool_id] = block.name
                    # Mirror the in-stream launch classification: an explicit
                    # run_in_background flag arms the background-launch gate for
                    # this launch's eventual terminal notification.
                    if tool_input.get("run_in_background") or tool_input.get("runInBackground"):
                        task_registry.mark_background_launch(session_id, tool_id)
                        logger.info(
                            "task-launch-classification session=%s tool_id=%s "
                            "classification=background-flag (cli-driven turn)",
                            session_id, tool_id,
                        )
                if block.name in ("Task", "Agent"):
                    agent_type = (
                        tool_input.get("subagent_type")
                        or tool_input.get("description")
                        or "Agent"
                    )
                    events.append({
                        "type": "subagent_start",
                        "tool_id": tool_id,
                        "agent_type": agent_type,
                        "description": tool_input.get("description", ""),
                        "prompt": tool_input.get("prompt", ""),
                        "agent_id": tool_id,
                    })
                else:
                    events.append({
                        "type": "tool_use",
                        "name": block.name,
                        "id": tool_id,
                        "input": tool_input,
                    })
        return events

    if isinstance(message, UserMessage):
        content = message.content
        if isinstance(content, str):
            # The CLI's injected user-lane steering text (the [SYSTEM
            # NOTIFICATION] wrapper around <task-notification>) — never rendered.
            return []
        for block in content or []:
            if isinstance(block, ToolResultBlock):
                tool_use_id = getattr(block, "tool_use_id", None)
                tool_name = tool_id_to_name.get(tool_use_id, "unknown")
                output = truncate_large_payload(block.content) if block.content else ""
                # Async Task/Agent launches (CLI 2.1.195+) omit run_in_background
                # and reveal backgroundness only in the launch-ACK ToolResult —
                # mirror the in-stream ACK sniff so the gate arms.
                if (
                    tool_use_id
                    and tool_name in ("Task", "Agent")
                    and _looks_like_subagent_launch_ack(output)
                ):
                    task_registry.mark_background_launch(session_id, tool_use_id)
                    logger.info(
                        "task-launch-classification session=%s tool_id=%s "
                        "classification=background-ack (cli-driven turn)",
                        session_id, tool_use_id,
                    )
                events.append({
                    "type": "tool_result",
                    "name": tool_name,
                    "tool_use_id": tool_use_id,
                    "output": output,
                    "is_error": bool(getattr(block, "is_error", False)),
                })
        return events

    return []


def _task_status_events_from_message(message: Any) -> List[Dict[str, Any]]:
    """Convert a drained task-status SystemMessage into the SAME ``system`` event
    the in-stream loop yields for it.

    RC2 (parked-state "Task Updated" chip): in-stream, a ``TaskUpdatedMessage``
    (a SystemMessage subclass) falls through to the generic SystemMessage branch
    in ``stream_to_websocket`` and yields
    ``{"type": "system", "subtype": <subtype>, "data": <data>}`` → ``handle_system``
    → ``add_system_message`` → live-store ``update_id`` bump → the chip renders.
    The between-turns drain pump had no equivalent, so a status patch that arrives
    while the session is PARKED was structurally undeliverable (the park-poller,
    keyed on ``update_id``, had nothing to fetch). This mirrors the in-stream shape
    so the pump can dispatch it through the identical handler chain.

    Only NON-init task-status system frames are converted (``init`` carries the SDK
    session id and is handled specially in-stream — never forwarded as a chip).
    ``TaskNotificationMessage`` / ``TaskProgressMessage`` / ``TaskStartedMessage``
    are NOT SystemMessage-shaped chips and are handled by their own pump branches,
    so they are excluded here. Returns ``[]`` for anything else.
    """
    if not isinstance(message, TaskUpdatedMessage):
        return []
    subtype = getattr(message, "subtype", None)
    if subtype == "init":
        return []
    return [{
        "type": "system",
        "subtype": subtype,
        "data": getattr(message, "data", None),
    }]


def _task_status_event_from_notification(message: Any) -> List[Dict[str, Any]]:
    """Synthesize a parked-state "Task Updated" chip from a terminal
    ``TaskNotificationMessage``.

    RC2 Part-2 gap: a NORMALLY-completed background sub-agent reports its terminal
    state BETWEEN turns as a ``TaskNotificationMessage`` (carrying status + summary
    + output_file) — NOT as a ``TaskUpdatedMessage``. In-stream that notification is
    consumed by ``handle_task_notification`` (which completes the sub-agent node but
    deliberately renders NO card), so the only user-visible completion chip comes
    from the ``task_updated`` SystemMessage branch. While PARKED there is no such
    branch, so a completion that arrives only as a notification produced no chip
    (the observed bug). This mirrors the in-stream ``task_updated`` chip shape from
    a notification so the drain pump can dispatch it through the identical
    ``handle_system`` -> ``add_system_message`` chain.

    The synthesized ``data`` matches exactly what the frontend
    ``SystemMessage.svelte`` ``task_updated`` case reads: ``task_id``, ``status``
    (also mirrored under ``patch.status`` since that branch prefers the patch), and
    ``summary`` (its description fallback). Returns ``[]`` for anything that is not
    a ``TaskNotificationMessage``.
    """
    if not isinstance(message, TaskNotificationMessage):
        return []
    status = getattr(message, "status", None)
    status_str = status.value if hasattr(status, "value") else (
        str(status) if status is not None else None
    )
    return [{
        "type": "system",
        "subtype": "task_updated",
        "data": {
            "task_id": getattr(message, "task_id", None),
            "status": status_str,
            "summary": getattr(message, "summary", None),
            # The frontend prefers patch.status; mirror it so the chip renders the
            # same "task xxx / <status>" content the in-stream task_updated card does.
            "patch": {"status": status_str},
        },
    }]


def _read_task_output_file(output_file: str, max_chars: int = 60_000) -> str:
    """Read a completed background task's full result off the local disk.

    The SDK's ``TaskNotificationMessage`` carries only a ``summary`` plus a path
    to ``output_file`` on the local host; the FULL sub-agent result lives in that
    file and there is no SDK helper to read it. This is the only way to obtain it.

    Fully defensive: the file may be missing or already cleaned up, so ANY error
    returns "" rather than raising. Oversize content is truncated via the shared
    ``truncate_large_payload`` helper (falls back to a simple slice).
    """
    if not output_file:
        return ""
    try:
        with open(output_file, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except Exception:
        logger.debug("failed to read task output_file %r", output_file, exc_info=True)
        return ""
    if len(content) <= max_chars:
        return content
    try:
        return truncate_large_payload(content, max_chars)
    except Exception:
        # Simple slice fallback if the shared helper is unavailable/changed.
        return content[:max_chars] + "\n…[truncated]"


def _task_notification_frame(message: Any) -> Dict[str, Any]:
    """Build the wire frame for a terminal ``TaskNotificationMessage``.

    Shared by both streaming paths so the enriched shape (output_file/uuid/usage
    alongside task_id/status/summary/tool_use_id) stays identical. ``usage`` is a
    TypedDict (a plain dict at runtime), guarded to ``None``.
    """
    usage = getattr(message, "usage", None)
    usage_data = None
    if usage:
        usage_data = {
            "total_tokens": usage.get("total_tokens", 0),
            "tool_uses": usage.get("tool_uses", 0),
            "duration_ms": usage.get("duration_ms", 0),
        }
    status = message.status
    return {
        "type": "task_notification",
        "task_id": message.task_id,
        "status": status.value if hasattr(status, "value") else str(status),
        "summary": message.summary,
        "tool_use_id": getattr(message, "tool_use_id", None),
        # Enrichment so the post-loop follow-up turn can read the full sub-agent
        # result off disk and de-dupe by uuid.
        "output_file": getattr(message, "output_file", None),
        "uuid": getattr(message, "uuid", None),
        "usage": usage_data,
    }


async def execute_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    automation_id: Optional[str] = None,
    persist_user_message: bool = True,
) -> Dict[str, Any]:
    """Execute a non-streaming query.

    ``automation_id`` stamps provenance onto a session this call creates, so
    the sessions list can badge automation-run sessions (they execute headless
    and read differently from interactive chats). Ignored when resuming an
    existing ``session_id``.
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
            api_user_id=api_user_id,
            automation_id=automation_id,
        )
        resume_id = None

        # Notify all connected frontends so the sidebar updates
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_created(
            user_id="admin",
            session_id=session_id,
            source_device_id=None
        )

    # Store user message with profile_id for per-query tracking. Skipped on a
    # failover retry (persist_user_message=False): the original entry already
    # persisted the user turn, so re-persisting would duplicate the message.
    if persist_user_message:
        await run_sync(
            database.add_session_message,
            session_id=session_id,
            role="user",
            content=prompt,
            profile_id=profile_id
        )

    # Keep the Claude OAuth token alive: refresh it now if it's at/near expiry so
    # the subprocess we're about to spawn/resume authenticates with a valid token
    # instead of 401-ing. No-op when the token is still valid or on API-key auth.
    await direct_oauth.ensure_fresh_credentials()

    # Build options with user-scoped credential resolution.
    # A WorktreeUnavailableError propagates here (non-streaming path) just like
    # the ValueErrors above — the caller turns it into an error response rather
    # than silently running the worktree session in the main checkout.
    options, agents_dict = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id,
    )

    # Resolve virtual @Notes/note-id references to actual content
    prompt = resolve_note_references(prompt)

    # Execute query and collect response
    response_text = []
    tool_messages = []  # Collect tool use/result messages for storage
    metadata = {}
    sdk_session_id = None
    sdk_link_persisted = False

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

    # Pull shared agent-memory from the DB before the CLI reads it.
    await reconcile_memory(project_id, getattr(options, "cwd", None))

    try:
        async for message in query(prompt=_prompt_stream(), options=options):
            # Mirror background-task lifecycle into the registry (TaskList / /live).
            # Task messages subclass SystemMessage, so this must run before the
            # isinstance chain claims them via the SystemMessage branch below.
            _record_task_message(session_id, message)
            if isinstance(message, SystemMessage):
                # session_id comes in warmup message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]
                    # Link it onto the sessions row immediately (decoupled from
                    # end-of-turn persistence), matching the streaming paths.
                    # The history read path keys the transcript store on
                    # sessions.sdk_session_id, and store entries flow in live —
                    # without this, a running (or mid-turn-crashed) automation
                    # session reads back as "no work" (blank history).
                    if resume_id is None and not sdk_link_persisted:
                        try:
                            await run_sync(
                                database.update_session,
                                session_id,
                                sdk_session_id=sdk_session_id,
                            )
                            sdk_link_persisted = True
                        except Exception as link_err:
                            logger.warning(
                                f"early sdk_session_id link failed for "
                                f"{session_id}: {link_err}"
                            )

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
                    elif _SERVER_TOOL_TYPES and isinstance(block, _SERVER_TOOL_TYPES):
                        # Server-executed tool call (advisor, web_search, web_fetch).
                        # Caller never runs these; we still surface them so the UI
                        # can badge "server tool" and the transcript is complete.
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": block.id,
                            "input": block.input,
                            "server_side": True,
                        })
                    elif _SERVER_TOOL_RESULT_TYPES and isinstance(block, _SERVER_TOOL_RESULT_TYPES):
                        # Server-tool result — content is an opaque dict, not str.
                        tool_messages.append({
                            "type": "tool_result",
                            "name": "server_tool",
                            "tool_id": getattr(block, 'tool_use_id', None),
                            "output": truncate_large_payload(block.content),
                            "server_side": True,
                        })
                metadata["model"] = message.model

                # Track per-API-call usage so we can persist the LAST call's
                # values (= current context window size). ResultMessage.usage
                # is cumulative across the agent loop and would inflate.
                if getattr(message, "usage", None):
                    metadata["last_call_tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["last_call_cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["last_call_cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

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
                # Headless/automation turns have no per-session WS stream to yield
                # this onto (unlike the streaming paths), so feed the live reading
                # into the usage monitor and broadcast a usage_snapshot so open
                # clients still reflect the change.
                await _apply_rate_limit_live_event(message.rate_limit_info)

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
                # Surface HTTP error status code (SDK 0.1.81+)
                api_err = getattr(message, "api_error_status", None)
                if api_err is not None:
                    metadata["api_error_status"] = api_err
                    # Funnel through the unified notification system so the
                    # non-streaming /query and /conversation paths reach the
                    # bell + every device, matching the streaming paths.
                    await _emit_api_error_notification(api_user_id, session_id, api_err)
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
        prompt=prompt,
        working_dir=getattr(options, "cwd", None),
    )

    # Push agent-memory the CLI wrote this turn up to the shared DB.
    await push_memory(project_id, getattr(options, "cwd", None))

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


async def _emit_api_error_notification(
    api_user_id: Optional[str],
    session_id: Optional[str],
    api_err: Any,
) -> None:
    """Funnel an SDK ``api_error`` through the unified notification system.

    Single source of truth for API-level errors (Anthropic 429 / 5xx): persists
    to the bell and broadcasts the user-scoped ``notification`` event so every
    device the user has open renders one consistent toast. The interactive
    "Fix with AI" button is rebuilt client-side (keyed on ``category`` —
    closures can't cross the wire), so we only ship a serializable action
    descriptor here. Never raises — must not break the streaming path.
    """
    try:
        from app.services.notification_service import emit_notification

        try:
            status = int(api_err)
        except (TypeError, ValueError):
            status = 0

        if status == 429:
            # If auto-switch-on-limit is eligible, the account_failover module
            # emits its own "switched to X" / "all accounts exhausted" toast that
            # supersedes this generic one — suppress the duplicate. Best-effort:
            # any failure here falls through to the generic toast below.
            try:
                from app.core import account_failover
                if await run_sync(account_failover.failover_eligible):
                    return
            except Exception:
                logger.debug("failover eligibility check failed in api_error toast", exc_info=True)
            severity = "warning"
            message = "Rate limited — slow down or wait for the next window."
            action = None  # transient + self-resolving — no "Fix with AI"
        elif 500 <= status < 600:
            severity = "error"
            message = f"Service error: HTTP {status}"
            action = {"kind": "fix_with_ai", "payload": {"status_code": status, "session_id": session_id}}
        elif status > 0:
            severity = "error"
            message = f"API error: HTTP {status}"
            action = {"kind": "fix_with_ai", "payload": {"status_code": status, "session_id": session_id}}
        else:
            severity = "error"
            message = "An API error occurred."
            action = {"kind": "fix_with_ai", "payload": {"session_id": session_id}}

        await emit_notification(
            api_user_id or "admin",
            severity=severity,
            message=message,
            category="api_error",
            surface="toast",
            session_id=session_id,
            persist=True,
            action=action,
        )
    except Exception:
        logger.debug("api_error notification emit failed", exc_info=True)


async def stream_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    api_user_id: Optional[str] = None,
    device_id: Optional[str] = None,  # Source device for sync
    persist_user_message: bool = True,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Execute a streaming query using ClaudeSDKClient.

    Following Anvil's SessionManager pattern:
    - Keep clients connected for session lifetime
    - Don't disconnect after each query (causes async context issues)
    - Reuse existing client if available, create new one with 'resume' if needed
    """

    # Get profile. Log at ERROR: consumers may have no surface for this yield
    # (headless path has no 'error' dispatch handler and force-cleans the live
    # store), so the log line can be the only trace of the refusal.
    profile = get_profile(profile_id)
    if not profile:
        logger.error(
            f"[WS] Profile not found: {profile_id} (session {session_id}) — refusing query"
        )
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if not project:
            logger.error(
                f"[WS] Project not found: {project_id} (session {session_id}) — refusing query"
            )
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

    # Store user message and broadcast to other devices. Skipped on a failover
    # retry (persist_user_message=False): the original entry already added the
    # user message to the DB, live store, and broadcast it — re-doing it here
    # would duplicate the message on every connected device.
    if persist_user_message:
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

    # Build options with user-scoped credential resolution.
    # WorktreeUnavailableError → refuse to run (never silently in main).
    try:
        options, agents_dict = build_options_from_profile(
            profile=profile,
            project=project,
            overrides=overrides,
            resume_session_id=resume_id,
            api_user_id=api_user_id,
            session_id=session_id
        )
    except (WorktreeUnavailableError, ValueError) as e:
        # Worktree unrecoverable, or the resolved working directory doesn't
        # exist (e.g. a project path from another install) — surface a clear
        # error instead of an opaque subprocess spawn failure.
        logger.error(f"[Query] Cannot build session options for {session_id}: {e}")
        yield {"type": "error", "message": str(e)}
        return

    # Yield init event
    yield {"type": "init", "session_id": session_id}

    # --- Session Pool: Acquire or create SDK client ---
    pool = get_session_pool()
    existing_state = pool.get(session_id)
    reusing_client = False

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        # Busy-write guard backstop: never write a second prompt into a client
        # that is mid-turn (kills the CLI, loses the whole in-flight turn).
        if existing_state.is_streaming:
            logger.warning(
                f"Pooled client for session {session_id} is mid-turn — "
                f"refusing concurrent query (busy-write guard)"
            )
            yield {
                "type": "error",
                "message": "Session is busy with another response. "
                           "Wait for it to finish, then send your message again.",
            }
            return
        if existing_state.stale_turn_undrained:
            # A force-cancelled turn's terminal ResultMessage is still buffered on
            # the pooled client's read loop; reusing it now would terminate this
            # query on that stale frame (message-crossing) or write into a possibly
            # mid-turn CLI. The between-turns drain pump clears the flag once it
            # consumes the stale frame; refuse until then.
            logger.warning(
                f"Pooled client for session {session_id} is winding down a "
                f"cancelled response — refusing concurrent query (stale-turn guard)"
            )
            yield {
                "type": "error",
                "message": "Session is still winding down a cancelled response; "
                           "try again shortly.",
            }
            return
        if not requires_reconnect(existing_state, options):
            state = existing_state
            reusing_client = True
            # CLAIM at the reuse decision point (no await since the is_streaming
            # check above) so two near-simultaneous entrants can't both pass the
            # gate. The finally below clears it on every exit path.
            state.is_streaming = True
            # Claim-to-try guard: from this claim until the main try/finally is
            # entered, ANY exit (exception or cancellation) must clear the flag
            # — a stranded is_streaming=True bricks the pooled client forever
            # (every future query gets the busy error, evict_idle skips
            # streaming states, report_long_streaming only logs).
            try:
                if credentials_rotated(state):
                    await refresh_subprocess_credentials(state, session_id)
                logger.info(f"Reusing pooled client for session {session_id}")
            except BaseException:
                state.is_streaming = False
                state.streaming_started_at = None
                raise
        else:
            logger.info(f"Options changed for session {session_id}, creating fresh client")
            await pool.remove(session_id)
            existing_state = None
    elif existing_state:
        logger.warning(f"Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # Admission backpressure: refuse a NEW session if the box is at its
        # process/RAM/pool ceiling, rather than killing in-flight work later.
        refusal = pool.admission_check()
        if refusal is not None:
            logger.warning(f"[Admission] Refusing new session {session_id}: {refusal}")
            yield {"type": "error", "message": f"Server is at capacity — {refusal}. Please retry shortly."}
            return

        # Evict LRU if pool is full
        await pool.evict_if_needed()

        logger.debug(f"Creating new ClaudeSDKClient for session {session_id} (resume={resume_id is not None})")

        # Attribute this session to the SDK's detached message-reader task, which
        # is created inside adapter.connect() below and inherits this context. A
        # "Fatal error in message reader" logged by that task then carries the
        # session id into error_log (see error_log_handler.emit).
        set_current_session_id(session_id)

        # Wrap stderr callback to collect lines for error reporting
        options.stderr, cli_stderr_lines = _wrap_stderr_capture(options.stderr)

        # Pull shared agent-memory from the DB before the CLI reads it.
        await reconcile_memory(project_id, getattr(options, "cwd", None))

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
            error_msg = _extract_connection_error(e, cli_stderr_lines)
            logger.error(f"User-facing error: {error_msg}")
            yield {"type": "error", "message": error_msg}
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
        # Persist-on-init guard (see stream_to_websocket): write the SDK session id
        # to the sessions row the moment it's captured so a mid-turn crash can't
        # orphan the session.
        sdk_link_persisted = False
        interrupted = False
    except BaseException:
        state.is_streaming = False
        state.streaming_started_at = None
        raise

    try:
        await state.client.query(prompt)

        async for message in state.client.receive_response():
            # Check typed task messages before generic SystemMessage
            # (they are subclasses and would match isinstance SystemMessage)
            # Mirror background-task lifecycle into the registry (TaskList / /live).
            _record_task_message(session_id, message)
            if isinstance(message, TaskNotificationMessage):
                # Mirror the enrichment from the WS path (stream_to_websocket) so
                # this REST-streaming consumer sees the same terminal-frame shape.
                yield _task_notification_frame(message)
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
                    # Link it onto the sessions row immediately (decoupled from
                    # end-of-turn persistence) so a mid-turn crash can't leave
                    # sdk_session_id NULL and orphan the session.
                    if resume_id is None and session_id and not sdk_link_persisted:
                        try:
                            await run_sync(
                                database.update_session,
                                session_id,
                                sdk_session_id=sdk_session_id,
                            )
                            sdk_link_persisted = True
                        except Exception as link_err:
                            logger.warning(
                                f"early sdk_session_id link failed for "
                                f"{session_id}: {link_err}"
                            )

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

                    elif _SERVER_TOOL_TYPES and isinstance(block, _SERVER_TOOL_TYPES):
                        # Server-executed tool call (advisor, web_search, web_fetch).
                        tool_id = block.id
                        if tool_id:
                            tool_id_to_name[tool_id] = block.name
                        yield {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input,
                            "server_side": True,
                        }
                        tool_messages.append({
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": tool_id,
                            "input": block.input,
                            "server_side": True,
                        })
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_use",
                            chunk_data={"name": block.name, "input": block.input, "server_side": True},
                            source_device_id=device_id
                        )

                    elif _SERVER_TOOL_RESULT_TYPES and isinstance(block, _SERVER_TOOL_RESULT_TYPES):
                        tool_use_id = getattr(block, 'tool_use_id', None)
                        tool_name = tool_id_to_name.get(tool_use_id, "server_tool")
                        output = truncate_large_payload(block.content)
                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "output": output,
                            "server_side": True,
                        }
                        tool_messages.append({
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output,
                            "server_side": True,
                        })
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=assistant_msg_id,
                            chunk_type="tool_result",
                            chunk_data={"name": tool_name, "output": output, "server_side": True},
                            source_device_id=device_id
                        )

                metadata["model"] = message.model

                # Per-turn usage from AssistantMessage (SDK 0.1.49+)
                if message.usage:
                    # Track per-API-call values (overwrite → last call wins).
                    # Used by _persist_turn_results as the real context-window
                    # size; ResultMessage.usage is cumulative and unusable here.
                    metadata["last_call_tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["last_call_cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["last_call_cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)
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
                _rli = message.rate_limit_info
                # Yield the frame to clients on THIS install's WS. Cross-install
                # views (peers, offloaded/cloud-relayed sessions) do NOT receive
                # this frame — the relay serves live_session_store, which never
                # carries rate_limit. They see the change only via the shared
                # usage_snapshots row + cluster broadcast we persist below.
                yield _rate_limit_event_dict(_rli, session_id)
                await _apply_rate_limit_live_event(_rli)

            elif isinstance(message, HookEventMessage):
                logger.debug(f"Hook event: {getattr(message, 'hook_event_name', None)}")
                yield {
                    "type": "hook_event",
                    "hook_event_name": getattr(message, "hook_event_name", None),
                    "matcher": getattr(message, "matcher", None),
                    "decision": getattr(message, "decision", None),
                    "reason": getattr(message, "reason", None),
                    "system_message": getattr(message, "system_message", None),
                    "tool_name": getattr(message, "tool_name", None),
                    "session_id": session_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
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
                # Surface HTTP error status code (SDK 0.1.81+); emit before result frame
                api_err = getattr(message, "api_error_status", None)
                if api_err is not None:
                    metadata["api_error_status"] = api_err
                    yield {"type": "api_error", "status": api_err, "session_id": session_id}
                    # Funnel through the unified notification system (persist + the
                    # bell + all the user's devices). The frontend `api_error`
                    # handler no longer toasts — the `notification` event does.
                    await _emit_api_error_notification(api_user_id, session_id, api_err)
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
        # Surface the subprocess's own stderr (exit code / stack trace it printed
        # before dying). Mid-stream the connect-scoped cli_stderr_lines is out of
        # scope, so without this the real exit-1 reason was only ever at DEBUG.
        log_captured_cli_stderr(session_id, state.cli_stderr_lines, context="rest-stream mid-stream")
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
        prompt=prompt,
        working_dir=getattr(options, "cwd", None),
    )

    # Push agent-memory the CLI wrote this turn up to the shared DB.
    await push_memory(project_id, getattr(options, "cwd", None))

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


async def stream_to_websocket(
    prompt: str,
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    broadcast_func: Optional[callable] = None,
    api_user_id: Optional[str] = None,
    user_id: Optional[str] = None
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
        user_id: Optional user ID for user-level broadcasts (e.g. lighting up the
            "awaiting input" indicator on every one of the user's devices)
    """
    # Get profile. Log at ERROR: consumers may have no surface for this yield
    # (headless path has no 'error' dispatch handler and force-cleans the live
    # store), so the log line can be the only trace of the refusal.
    profile = get_profile(profile_id)
    if not profile:
        logger.error(
            f"[WS] Profile not found: {profile_id} (session {session_id}) — refusing query"
        )
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = await run_sync(database.get_project, project_id)
        if not project:
            logger.error(
                f"[WS] Project not found: {project_id} (session {session_id}) — refusing query"
            )
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

    # Keep the Claude OAuth token alive: refresh it now if it's at/near expiry so
    # the subprocess we're about to spawn/resume authenticates with a valid token
    # instead of 401-ing. No-op when the token is still valid or on API-key auth.
    await direct_oauth.ensure_fresh_credentials()

    # Build options once (without callback — callback is set later if needed)
    _build_opts_start = time.monotonic()
    try:
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
    except (WorktreeUnavailableError, ValueError) as e:
        # A worktree session whose worktree can't be recovered, or a working
        # directory that doesn't exist (e.g. a project path from another
        # install): refuse to run in the main checkout / with an invalid cwd.
        # Surface a clear error to the user instead.
        logger.error(f"[WS] Cannot build session options for {session_id}: {e}")
        yield {"type": "error", "message": str(e)}
        return
    _build_opts_elapsed = time.monotonic() - _build_opts_start
    if _build_opts_elapsed > 0.5:
        logger.warning(f"[WS] build_options_from_profile took {_build_opts_elapsed:.2f}s for session {session_id}")

    if existing_state and existing_state.is_connected and is_subprocess_alive(existing_state):
        # Busy-write guard backstop: a client that is mid-turn must NEVER get a
        # second prompt written into its stdin — the CLI dies with "Fatal error
        # in message reader" and the whole in-flight turn is lost. The WS
        # handlers enqueue instead of racing us, but this gate is the last line
        # of defence for any caller (REST, background, unpark races).
        if existing_state.is_streaming:
            logger.warning(
                f"[WS] Pooled client for session {session_id} is mid-turn — "
                f"refusing concurrent query (busy-write guard)"
            )
            yield {
                "type": "error",
                "message": "Session is busy with another response. "
                           "Wait for it to finish, then send your message again.",
            }
            return
        if existing_state.stale_turn_undrained:
            # See stream_query: a force-cancelled turn's terminal ResultMessage is
            # still buffered on the pooled client; the drain pump clears this flag
            # once consumed. Refuse until then so we never cross the stale frame.
            logger.warning(
                f"[WS] Pooled client for session {session_id} is winding down a "
                f"cancelled response — refusing concurrent query (stale-turn guard)"
            )
            yield {
                "type": "error",
                "message": "Session is still winding down a cancelled response; "
                           "try again shortly.",
            }
            return
        # Candidate for reuse — check if critical options changed
        if not requires_reconnect(existing_state, options):
            # REUSE: Subprocess is alive and options are compatible
            state = existing_state
            reusing_client = True

            # CLAIM the client at the reuse decision point. No await between the
            # is_streaming check above and this line (requires_reconnect is
            # synchronous), so two near-simultaneous entrants can't both pass
            # the gate — the loser sees is_streaming=True and fails safe. The
            # full "Mark as streaming" block below re-stamps timestamps; the
            # try/finally around the query clears the flag on every exit path.
            state.is_streaming = True

            # Claim-to-try guard: from this claim until the main try/finally is
            # entered, ANY exit (exception or cancellation — the refresh and
            # set_model awaits below are suspension points) must clear the flag.
            # A stranded is_streaming=True bricks the pooled client forever:
            # every future query gets the busy error, evict_idle skips
            # streaming states, and report_long_streaming only logs.
            try:
                # OAuth rotated on disk since spawn → sync it into the subprocess's
                # config dir in place. Respawning here would SIGTERM the subprocess
                # and kill every background task/sub-agent it owns.
                if credentials_rotated(state):
                    await refresh_subprocess_credentials(state, session_id)

                # Update callback refs so the existing can_use_tool closure sees current values
                state.callback_refs.update({
                    "broadcast_func": broadcast_func,
                    "permission_mode": permission_mode,
                    "allowed_tools": allowed_tools,
                    "disallowed_tools": disallowed_tools,
                    "profile_id": profile_id,
                    "config": config,
                    "session_id": session_id,
                    "user_id": user_id,
                    # Reset the outstanding-prompt counter at the start of each turn so a
                    # prior turn that was hard-cancelled mid-prompt (its finally clear
                    # interrupted) can't leave a leaked count that pins the indicator on.
                    "attention_count": 0,
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
            except BaseException:
                state.is_streaming = False
                state.streaming_started_at = None
                raise
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
            "user_id": user_id,
            # Reference count of prompts (questions + permissions) this session is
            # currently blocked on. Drives the user-level "awaiting input" indicator.
            "attention_count": 0,
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
                _user_id = refs.get("user_id")

                # Extract tool_use_id and agent_id from context (SDK 0.1.52+)
                # These enable disambiguating parallel permission requests
                _ctx_tool_use_id = getattr(context, 'tool_use_id', None)
                _ctx_agent_id = getattr(context, 'agent_id', None)

                # Extract SDK 0.1.81 ToolPermissionContext enrichment fields (Slice D)
                _ctx_decision_reason = getattr(context, 'decision_reason', None)
                _ctx_blocked_path    = getattr(context, 'blocked_path', None)
                _ctx_title           = getattr(context, 'title', None)
                _ctx_display_name    = getattr(context, 'display_name', None)
                _ctx_description     = getattr(context, 'description', None)

                # Check profile-level tool restrictions FIRST
                if _disallowed and tool_name in _disallowed:
                    logger.debug(f"[WS] Tool {tool_name} blocked by profile disallowed_tools")
                    return PermissionResultDeny(message=f"Tool '{tool_name}' is disabled in this profile")
                if _allowed and tool_name not in _allowed:
                    logger.debug(f"[WS] Tool {tool_name} blocked by profile allowed_tools (not in whitelist)")
                    return PermissionResultDeny(message=f"Tool '{tool_name}' is not enabled in this profile")

                # User-level "awaiting input" indicator. The per-socket question /
                # permission event only reaches the tab that owns this query; fanning
                # a session_attention event out to every one of the user's devices
                # lets the sessions board show which session is blocked on a prompt,
                # no matter which tab the user is looking at.
                async def _bump_attention(delta: int, kind: str):
                    """Adjust this session's outstanding-prompt count and fan the new
                    total out to every one of the user's devices. Reference-counted
                    (not absolute) because a session can be blocked on several prompts
                    at once — the SDK issues parallel tool calls, so two permission
                    prompts (or a question + a permission) can be open simultaneously.
                    If each prompt independently broadcast 0 on resolve, the first to
                    finish would clear the indicator while its siblings were still
                    pending. Counting up on enter and down on exit keeps the dot lit
                    until the LAST prompt resolves. The read-modify-write is safe
                    without a lock: it runs to completion before the first await, and
                    the event loop is single-threaded."""
                    if not _user_id:
                        return
                    n = max(0, (callback_refs.get("attention_count") or 0) + delta)
                    callback_refs["attention_count"] = n
                    try:
                        await sync_engine.broadcast_session_attention(
                            _user_id, _sid, n, kind=kind
                        )
                    except Exception:
                        logger.debug("[WS] session_attention broadcast failed", exc_info=True)

                async def _request_permission(**kwargs):
                    """Show a permission prompt, wrapped with the awaiting-input
                    indicator so a backgrounded session blocked on approval is just
                    as discoverable as one blocked on a question. Clears the
                    indicator whether the prompt is answered, deferred, or the query
                    is cancelled (session closed)."""
                    await _bump_attention(1, "permission")
                    try:
                        return await permission_handler.request_permission(**kwargs)
                    finally:
                        await _bump_attention(-1, "permission")

                # Handle AskUserQuestion specially
                if tool_name == "AskUserQuestion":
                    questions = tool_input.get("questions", [])
                    if not questions:
                        return PermissionResultAllow(updated_input=tool_input)

                    request_id = f"question-{_sid}-{uuid.uuid4().hex[:8]}"
                    logger.debug(f"[WS] AskUserQuestion canUseTool triggered: {request_id} with {len(questions)} questions (tool_use_id={_ctx_tool_use_id}, agent_id={_ctx_agent_id})")

                    # Fan the question to every device watching this session, not
                    # just the origin socket. The origin _broadcast reaches only the
                    # tab that owns this query — nobody on a headless turn, a socket
                    # that died on reconnect, or a query driven from another device.
                    # Mirrors how _bump_attention fans session_attention to all the
                    # user's devices; the frontend dedupes by request_id.
                    async def _fanout_question(payload):
                        if not _user_id:
                            return
                        await sync_engine.broadcast_user_question(_user_id, payload)

                    await _bump_attention(1, "question")
                    try:
                        answers = await user_question_handler.request_answers(
                            request_id=request_id,
                            tool_use_id=_ctx_tool_use_id or request_id,
                            session_id=_sid,
                            questions=questions,
                            broadcast_func=_broadcast,
                            fanout_func=_fanout_question
                        )
                    finally:
                        # Decrement whether the question was answered, timed out, or
                        # the query was cancelled (session closed). The indicator only
                        # clears once every outstanding prompt in this session resolves.
                        await _bump_attention(-1, "question")

                    logger.debug(f"[WS] Got answers for {request_id}: {list(answers.keys())}")
                    return PermissionResultAllow(updated_input={**tool_input, "answers": answers})

                # ExitPlanMode handling — show approval modal in default mode
                # only. In bypass mode, auto-approve so fire-and-forget
                # workflows aren't blocked waiting for user input.
                if tool_name == "ExitPlanMode":
                    if _perm_mode != "bypassPermissions":
                        request_id = f"perm-{_sid}-{uuid.uuid4().hex[:8]}"
                        logger.debug(f"[WS] ExitPlanMode requires approval in {_perm_mode} mode: {request_id}")

                        result = await _request_permission(
                            request_id=request_id,
                            session_id=_sid,
                            profile_id=_profile_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            broadcast_func=_broadcast,
                            tool_use_id=_ctx_tool_use_id,
                            agent_id=_ctx_agent_id,
                            title=_ctx_title,
                            display_name=_ctx_display_name,
                            description=_ctx_description,
                            decision_reason=_ctx_decision_reason,
                            blocked_path=_ctx_blocked_path,
                        )
                        if isinstance(result, _DeferSentinel):
                            # Deferred ExitPlanMode — treat as deny so execution
                            # stays paused; user can requeue and re-approve later
                            return PermissionResultDeny(message="ExitPlanMode deferred by user")
                        if result.behavior == "deny":
                            return result

                    return PermissionResultAllow()

                # Computer use: request_access is the single approval gate (keyed to
                # permission mode); individual actions are auto-allowed here — they
                # enforce the granted state themselves, so we never prompt per click.
                if tool_name.startswith("mcp__computer-use__"):
                    if tool_name == "mcp__computer-use__request_access":
                        if _perm_mode == "bypassPermissions":
                            return PermissionResultAllow()
                        if _perm_mode == "plan":
                            return PermissionResultDeny(message="Computer control is disabled in plan mode.")
                        apps = tool_input.get("apps") or []
                        reason = tool_input.get("reason") or ""
                        request_id = f"perm-{_sid}-{uuid.uuid4().hex[:8]}"
                        desc = "Claude wants to control: " + (", ".join(apps) if apps else "your screen")
                        if reason:
                            desc += f"\nReason: {reason}"
                        result = await _request_permission(
                            request_id=request_id,
                            session_id=_sid,
                            profile_id=_profile_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            broadcast_func=_broadcast,
                            tool_use_id=_ctx_tool_use_id,
                            agent_id=_ctx_agent_id,
                            title="Computer control access",
                            display_name="Control your computer",
                            description=desc,
                        )
                        if isinstance(result, _DeferSentinel):
                            return PermissionResultDeny(message="Computer-use access deferred by user")
                        if getattr(result, "behavior", None) == "deny":
                            return result
                        return PermissionResultAllow()
                    # All other computer-use actions: access is gated by request_access.
                    return PermissionResultAllow()

                # For other tools in default mode, route through permission handler
                if _perm_mode == "default":
                    request_id = f"perm-{_sid}-{uuid.uuid4().hex[:8]}"
                    logger.debug(f"[WS] Permission request for {tool_name}: {request_id}")

                    result = await _request_permission(
                        request_id=request_id,
                        session_id=_sid,
                        profile_id=_profile_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        broadcast_func=_broadcast,
                        tool_use_id=_ctx_tool_use_id,
                        agent_id=_ctx_agent_id,
                        title=_ctx_title,
                        display_name=_ctx_display_name,
                        description=_ctx_description,
                        decision_reason=_ctx_decision_reason,
                        blocked_path=_ctx_blocked_path,
                    )
                    if isinstance(result, _DeferSentinel):
                        # SDK 0.1.81 — return PreToolUseHookSpecificOutput with defer
                        try:
                            from claude_agent_sdk.types import PreToolUseHookSpecificOutput
                            return PreToolUseHookSpecificOutput(
                                hookEventName="PreToolUse",
                                permissionDecision="defer",
                                permissionDecisionReason="Deferred by user",
                            )
                        except (ImportError, TypeError):
                            # Fallback: if SDK signature differs, treat as deny so we never crash
                            return PermissionResultDeny(message="Tool deferred by user")
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

        # Admission backpressure: refuse a NEW session if the box is at its
        # process/RAM/pool ceiling, rather than killing in-flight work later.
        refusal = pool.admission_check()
        if refusal is not None:
            logger.warning(f"[Admission] Refusing new session {session_id}: {refusal}")
            yield {"type": "error", "message": f"Server is at capacity — {refusal}. Please retry shortly."}
            return

        # Evict LRU session if pool is full
        evicted = await pool.evict_if_needed()
        if evicted:
            logger.debug(f"[WS] Evicted LRU session {evicted} to make room for {session_id}")

        # Create and connect new client
        logger.debug(f"[WS] Creating ClaudeSDKClient for session {session_id} (resume={resume_id is not None}, include_partial={options.include_partial_messages})")
        logger.debug(f"[WS] Options cwd: {options.cwd}")

        # Verify CWD exists before spawning subprocess
        if options.cwd and not Path(options.cwd).is_dir():
            logger.error(f"[WS] CWD does not exist: {options.cwd}")
            yield {"type": "error", "message": f"Working directory does not exist: {options.cwd}"}
            return

        # Attribute this session to the SDK's detached message-reader task (see
        # the chat path above / error_log_handler.emit).
        set_current_session_id(session_id)

        # Wrap stderr callback to collect lines for error reporting
        options.stderr, cli_stderr_lines = _wrap_stderr_capture(options.stderr)

        # Pull shared agent-memory from the DB into the local memory dir before
        # the CLI reads it, so this install sees memories written elsewhere.
        await reconcile_memory(project_id, options.cwd)

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
            error_msg = _extract_connection_error(e, cli_stderr_lines)
            logger.error(f"[WS] User-facing error: {error_msg}")
            yield {"type": "error", "message": error_msg}
            return

        state = SessionState(
            adapter=adapter,
            sdk_session_id=resume_id,
            is_connected=True,
            callback_refs=callback_refs,
            connect_options=options,
            credentials_mtime=claude_credentials_mtime(),
            cli_stderr_lines=cli_stderr_lines,
        )
        pool.put(session_id, state)

    # Mark as streaming (both reuse and create paths)
    state.is_streaming = True
    state.interrupt_requested = False  # Reset from previous query's interrupt
    state.streaming_started_at = datetime.now(timezone.utc)
    state.last_activity = datetime.now(timezone.utc)

    # Claim-to-try guard: is_streaming is set but the protective try/finally
    # hasn't started yet — any exit in between must clear the flag or the
    # pooled client is bricked (busy error forever, never idle-evicted).
    try:
        # Make this session's node subprocess (and the subagents it is about to fork,
        # which inherit the score) the kernel's preferred OOM-kill target. If the HOST
        # runs out of memory mid-fan-out, the global OOM-killer then reaps one session's
        # disposable, auto-recoverable subprocess instead of the Python orchestrator
        # (which runs every session) or the database. Best-effort; set every turn,
        # before the query() below triggers the fan-out. No-op if disabled/unprivileged.
        mark_subprocess_oom_killable(state)

        # Initialize sdk_session_id - use resume_id if resuming, otherwise None until we get it from SDK
        sdk_session_id = resume_id
        # Persist-on-init guard: a brand-new session's sdk_session_id is only known
        # once the SDK emits its `init` SystemMessage. We write it to the sessions
        # row immediately (once) so a mid-turn crash can't orphan the session.
        sdk_link_persisted = False
        # Set when the turn dies via `except Exception`; suppresses the trailing
        # `done` frame (we already emitted `error`) while persistence still runs.
        crashed = False

        # Execute query
        # Note: SDK built-in commands like /context and /compact are passed directly to the SDK
        response_text = []
        tool_messages = []  # Collect tool use/result messages for storage
        thinking_blocks = []  # Collect thinking blocks for DB persistence
        system_messages_acc = []  # Collect system messages for DB persistence
        plugin_messages_acc = []  # Collect plugin/hook warnings for DB persistence
        # Preserve stream order for DB persistence. Each entry is tagged with a
        # `kind` field so query_persistence can emit rows in the exact sequence
        # they arrived, instead of bucketing all-thinking→all-tools→all-text.
        turn_events: list = []
        # Subagent (Task/Agent) child tool invocations keyed by parent_tool_id.
        # These are embedded into the parent subagent's agent_children metadata
        # at persistence time so reloading a session reconstructs the subagent's
        # internal tool usage. Child tool_use entries are NOT persisted as
        # top-level rows (they'd be orphaned without their parent context).
        subagent_children: Dict[str, List[Dict[str, Any]]] = {}
        task_tool_uses = {}  # Track Task (subagent) tool uses by tool_id
        # Seed cross-turn BACKGROUND sub-agents. Background sub-agents
        # (run_in_background=true) OUTLIVE the turn that launched them: in a
        # FOLLOW-UP turn their StreamEvents/AssistantMessages still arrive with
        # parent_tool_use_id set, but the launching Agent/Task tool_use block was
        # seen in a PRIOR turn and is absent from this fresh empty dict. Lane
        # routing itself no longer depends on this seed — every subagent gate now
        # routes on a non-null parent_tool_use_id alone (parent-null-based), so
        # foreign events reach the subagent lane whether or not their launch id is
        # seeded. The seed still has two consumers: (a) the
        # `tool_use_id in task_tool_uses` launch-completion/ACK branches
        # (~2920/~3185) — a seeded entry (run_in_background=True) makes a cross-turn
        # launch ACK skip subagent_done and keep the node RUNNING; and (b) the
        # SystemMessage `len(task_tool_uses) > 0` suppression heuristic, which treats
        # system messages as subagent context while any tracked task is still live.
        # Union the durable _background_launches set (cleared only with the session)
        # with the parent tool_use ids of all in-flight background work
        # (get_active_background_work — the same view the pump-start guard uses), to
        # catch a launch whose tool_use_id both arming heuristics missed (the
        # run_in_background input flag AND the ACK text-sniff) but which is still a
        # RUNNING task. Seeded entries carry run_in_background=True and _cross_turn
        # so late children are NOT re-persisted as this turn's subagent metadata
        # (their parent row was persisted in a prior turn — see subagent_children).
        _seed_bg_ids = set(task_registry.get_background_launches(session_id))
        for _work in task_registry.get_active_background_work(session_id):
            _work_tool_id = _work.get("toolUseId")
            if _work_tool_id:
                _seed_bg_ids.add(_work_tool_id)
        for _bg_tool_id in _seed_bg_ids:
            task_tool_uses[_bg_tool_id] = {
                "agent_type": "Agent",
                "description": "",
                "prompt": "",
                "run_in_background": True,
                "_cross_turn": True,
            }
        tool_id_to_name = {}  # Track tool_use_id → tool_name for ToolResultBlock lookup
        metadata = {}
        interrupted = False
        include_partial = options.include_partial_messages  # Track if streaming events are enabled
        has_visible_content = False  # Track if any content was yielded to the frontend
        is_slash_command = prompt.strip().startswith("/")
        _current_thinking = []  # Accumulate thinking deltas into blocks
        _current_text_parts: list = []  # Accumulate asst text between blocks for ordered persistence

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
    except BaseException:
        state.is_streaming = False
        state.streaming_started_at = None
        raise

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
                # Accumulate for DB persistence
                if stderr_msg.get("type") == "plugin_message":
                    plugin_messages_acc.append(stderr_msg)
                    turn_events.append({"kind": "plugin", "payload": stderr_msg})
                yield stderr_msg

            # Check typed task messages before generic SystemMessage
            # (they are subclasses and would match isinstance SystemMessage)
            # Mirror background-task lifecycle into the registry (TaskList / /live).
            _record_task_message(session_id, message)

            # STREAM_DEBUG: trace SDK frame ordering ONLY while a background task
            # is active for this session, so a live test can confirm the exact
            # ordering (the terminal TaskNotificationMessage arrives in-stream
            # immediately before the ResultMessage that ends this loop). Fully
            # gated + cheap: the env lookup is the only cost when disabled.
            if _STREAM_DEBUG and session_id and task_registry.get_active(session_id):
                logger.info(
                    "[STREAM_DEBUG] t=%.3f session=%s frame=%s",
                    time.monotonic(),
                    session_id,
                    type(message).__name__,
                )

            if isinstance(message, TaskNotificationMessage):
                # Only count this as visible content when there's an actual
                # summary to show — an empty summary would otherwise force a
                # spurious empty card (Bug 2 artifact).
                if message.summary:
                    has_visible_content = True
                yield _task_notification_frame(message)
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
                    # Link the SDK session id onto the sessions row the instant
                    # it's known — decoupled from end-of-turn _persist_turn_results.
                    # If the turn later crashes (OOM/SIGKILL during a subagent
                    # fan-out), the old `except ... return` skipped persistence and
                    # left sdk_session_id NULL, orphaning the whole session (blank
                    # history in postgres_only mode). The session store entries are
                    # already keyed by this id, so writing it here alone is enough
                    # to recover the full transcript on reload.
                    if resume_id is None and not sdk_link_persisted:
                        try:
                            await run_sync(
                                database.update_session,
                                session_id,
                                sdk_session_id=sdk_session_id,
                            )
                            sdk_link_persisted = True
                        except Exception as link_err:
                            logger.warning(
                                f"[WS] early sdk_session_id link failed for "
                                f"{session_id}: {link_err}"
                            )

                else:
                    # Forward all system messages to frontend except noisy internal ones.
                    # Previous whitelist approach silently dropped SDK errors/warnings
                    # with new subtypes (e.g., skill errors, command output).
                    # Also suppress the transient subtypes that the live WS
                    # handler routes into other UI elements (thinking counter,
                    # subagent progress) and never renders as a card. Without
                    # this, every new session persists useless rows to the DB
                    # (e.g. thinking_tokens). The read-side filter
                    # (system_message_filter) is still required for rows already
                    # written by older sessions; sourcing the set from
                    # HIDDEN_SYSTEM_SUBTYPES keeps the two lists from drifting.
                    SUPPRESSED_SUBTYPES = {
                        'init',  # Already handled above
                        'rate_limit_event',  # Internal API rate limit info, not useful in chat
                    } | HIDDEN_SYSTEM_SUBTYPES

                    # Suppress system messages from subagent execution.
                    # SystemMessage doesn't have parent_tool_use_id as a direct attribute,
                    # but the raw data dict may contain it. Also suppress if any subagents
                    # are actively running (SDK runs subagents synchronously, so system
                    # messages during active subagent execution are from the subagent context).
                    parent_tool_id = message.data.get("parent_tool_use_id") if message.data else None
                    # The main agent's own messages always carry parent_tool_use_id=None,
                    # so any non-null parent is a subagent event and must never route to
                    # the main lane — even when the launch id is absent from task_tool_uses.
                    is_subagent_lane = parent_tool_id is not None
                    is_from_subagent = is_subagent_lane or len(task_tool_uses) > 0

                    # Suppress no-op "status" system messages emitted by the SDK
                    # on every stream_request_start (status="requesting"). These
                    # are internal telemetry with no user-visible value and were
                    # cluttering the chat between tools. Keep meaningful status
                    # updates (compacting, permissionMode changes).
                    data_dict = message.data if isinstance(message.data, dict) else {}
                    status_val = data_dict.get("status")
                    is_noop_status = (
                        message.subtype == "status"
                        and status_val in (None, "", "requesting")
                        and not data_dict.get("permissionMode")
                    )

                    if is_from_subagent:
                        logger.debug(f"[WS] Suppressed subagent SystemMessage: subtype={message.subtype}")
                    elif is_noop_status:
                        logger.debug(f"[WS] Suppressed no-op status SystemMessage: status={status_val}")
                    elif message.subtype not in SUPPRESSED_SUBTYPES:
                        has_visible_content = True
                        logger.debug(f"[WS] Forwarding SystemMessage to frontend: subtype={message.subtype}")
                        # Accumulate for DB persistence
                        sys_content = ""
                        if isinstance(message.data, dict) and "content" in message.data:
                            sys_content = str(message.data["content"])
                        sys_entry = {
                            "content": sys_content,
                            "subtype": message.subtype,
                            "data": message.data,
                        }
                        system_messages_acc.append(sys_entry)
                        turn_events.append({"kind": "system", "payload": sys_entry})
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
                is_subagent_lane = parent_tool_id is not None

                # Route subagent StreamEvents — don't mix into main agent stream.
                # The SDK (0.1.30+) sets parent_tool_use_id on subagent events.
                if is_subagent_lane:
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
                        # Finalize any accumulated thinking block
                        if _current_thinking:
                            thinking_entry = {"content": "".join(_current_thinking)}
                            thinking_blocks.append(thinking_entry)
                            turn_events.append({"kind": "thinking", "payload": thinking_entry})
                            _current_thinking.clear()
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
                        # Thinking block streaming — accumulate for DB persistence
                        _current_thinking.append(delta["thinking"])
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
                is_subagent_lane = parent_tool_id is not None

                # Capture the JSONL transcript uuid for this AssistantMessage.
                # The uuid is the same for every block inside this message and
                # lets rewind map a DB row back to a transcript entry. Track the
                # most recent non-subagent assistant uuid in metadata so the
                # final assistant row inherits it at persistence time.
                asst_uuid = getattr(message, "uuid", None)
                if asst_uuid and not parent_tool_id:
                    metadata["last_assistant_uuid"] = asst_uuid

                for block in message.content:
                    if isinstance(block, TextBlock):
                        # Check if this text is from a subagent.
                        if is_subagent_lane:
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
                            # Record this text block in ordered turn events so it
                            # persists in the exact stream position relative to
                            # tool_use blocks. Without this, all text was bucketed
                            # into a single merged assistant row appended AFTER the
                            # tools, which made reloads (DB fast-path) render the
                            # tool card above the text and merge separate text
                            # blocks of a text→tool→text turn into one bubble.
                            if block.text and block.text.strip():
                                turn_events.append({
                                    "kind": "text",
                                    "payload": {
                                        "content": block.text,
                                        "uuid": asst_uuid,
                                    },
                                })
                            if not include_partial:
                                logger.debug(f"[WS] Text chunk len={len(block.text)} for session={session_id}")
                                yield {"type": "chunk", "content": block.text}

                    elif isinstance(block, ToolUseBlock):
                        tool_id = getattr(block, 'id', None)
                        tool_input = block.input or {}

                        # Track tool_id → name for ToolResultBlock lookup
                        if tool_id:
                            tool_id_to_name[tool_id] = block.name

                        # Tag tool_messages entries with parent_tool_id so persistence
                        # can identify subagent children and embed them into the parent
                        # subagent's agent_children metadata instead of emitting them
                        # as orphaned top-level rows.
                        tool_entry = {
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": tool_id,
                            "input": tool_input,
                            # non-null parent = subagent child; persistence skips top-level emission
                            "parent_tool_id": parent_tool_id,
                            "uuid": asst_uuid,
                        }
                        tool_messages.append(tool_entry)

                        # Gate for the spurious-unpark fix: record the launch
                        # tool_use_id IFF the parent launched this tool with
                        # run_in_background=true — Agent sub-agents and
                        # Bash/PowerShell shells alike. The SDK puts this SAME id
                        # on the eventual TaskStartedMessage / at-rest
                        # TaskNotificationMessage, so _record_task_message and the
                        # unpark paths can confirm a task belongs to a genuinely-
                        # backgrounded launch. A FOREGROUND call's result returns
                        # INLINE as a tool_result this turn and must NOT arm the
                        # unpark gate. Top-level launches only: a sub-agent's own
                        # backgrounded grandchild must not unpark or pin the
                        # parent. Side-effect-only — does not change what is
                        # yielded.
                        if (
                            tool_id
                            and not tool_entry["parent_tool_id"]
                            and (
                                tool_input.get("run_in_background")
                                or tool_input.get("runInBackground")
                            )
                        ):
                            task_registry.mark_background_launch(session_id, tool_id)
                            logger.info(
                                "task-launch-classification session=%s tool_id=%s "
                                "classification=background-flag",
                                session_id, tool_id,
                            )
                        elif tool_id and not tool_entry["parent_tool_id"] and block.name in ("Task", "Agent"):
                            # A top-level Agent/Task launch WITHOUT the input flag: it
                            # is provisionally foreground here. The CLI (2.1.195+) may
                            # still reveal it as async via its launch-ACK ToolResult,
                            # which re-classifies it as background-ack below.
                            logger.info(
                                "task-launch-classification session=%s tool_id=%s "
                                "classification=foreground",
                                session_id, tool_id,
                            )
                        elif (
                            tool_id
                            and tool_entry["parent_tool_id"]
                            and (
                                tool_input.get("run_in_background")
                                or tool_input.get("runInBackground")
                            )
                        ):
                            # SUBAGENT-lane backgrounded launch (a grandchild).
                            # Recorded in a DEDICATED set — never
                            # mark_background_launch, which would pin the pool /
                            # chip it as the parent's own work — so the drain
                            # pump can still deliver its terminal notification
                            # instead of suppress-foreground'ing it and
                            # stranding a sub-agent parked awaiting the task
                            # (2026-07-10 incident; the #470 top-level-only
                            # gate above is what kept it invisible).
                            task_registry.mark_subagent_background_launch(session_id, tool_id)
                            logger.info(
                                "task-launch-classification session=%s tool_id=%s "
                                "classification=background-flag-subagent-lane",
                                session_id, tool_id,
                            )
                        elif (
                            tool_id
                            and tool_entry["parent_tool_id"]
                            and block.name in _IMPLICIT_BACKGROUND_TOOL_NAMES
                        ):
                            # SUBAGENT-lane FLAG-LESS background builtin
                            # (Monitor): always spawns a background task but
                            # carries no run_in_background flag, so the branch
                            # above can never classify it (live 2026-07-10,
                            # session 07df5136 task b5qiqqf1h → pump
                            # decision=suppress-foreground, stranded sub-agent).
                            # Same dedicated set as above — never
                            # mark_background_launch (no pool pin, no chip
                            # scope; #470 preserved). TOP-LEVEL Monitors fall
                            # through unclassified on purpose: their wakes are
                            # CLI-owned (queue enqueue + self-dequeue) and the
                            # app must keep suppressing them.
                            task_registry.mark_subagent_background_launch(session_id, tool_id)
                            logger.info(
                                "task-launch-classification session=%s tool_id=%s "
                                "classification=background-builtin-subagent-lane",
                                session_id, tool_id,
                            )

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
                                "prompt": tool_input.get("prompt", ""),
                                # Backgrounded sub-agents (run_in_background) return a
                                # launch-ACK ToolResult immediately; that ack must NOT
                                # complete the subagent node (the real result arrives
                                # later via TaskNotificationMessage). Track the flag so
                                # the ToolResult branches below can skip subagent_done.
                                "run_in_background": bool(
                                    tool_input.get("run_in_background")
                                    or tool_input.get("runInBackground")
                                ),
                            }
                            # Initialize children collector for this subagent
                            subagent_children[tool_id] = []
                            # Record subagent in ordered turn events for persistence
                            turn_events.append({"kind": "subagent", "payload": tool_entry})

                            # Yield subagent_start event
                            yield {
                                "type": "subagent_start",
                                "tool_id": tool_id,
                                "agent_type": agent_type,
                                "description": tool_input.get("description", ""),
                                "prompt": tool_input.get("prompt", ""),
                                "agent_id": tool_id
                            }
                        # Check if this tool use is from a subagent. A missing live
                        # subagent node just drops the child card (acceptable degrade).
                        elif is_subagent_lane:
                            if parent_tool_id not in task_tool_uses:
                                logger.debug(f"[WS] Routing event with unknown parent_tool_use_id={parent_tool_id} to subagent lane (not in task_tool_uses)")
                            # Record as child of the parent subagent (not top-level).
                            # For a CROSS-TURN background parent (seeded at turn
                            # start, flagged _cross_turn), the parent's subagent row
                            # was already persisted in a PRIOR turn, so there is no
                            # "subagent" turn_event this turn to embed these children
                            # into. That is intentional: this child is tagged with
                            # parent_tool_id below, so persistence SKIPS it as a
                            # top-level row (no orphan), and its collector is never
                            # embedded (dropped from persistence) — the live/registry
                            # subagent_tool_use event below is the only surface it
                            # needs. It must NEVER reach the main-lane tool handler.
                            child_entry = {
                                "id": tool_id,
                                "type": "tool_use",
                                "content": "",
                                "streaming": False,
                                "tool_name": block.name,
                                "tool_id": tool_id,
                                "tool_input": tool_input,
                                "tool_result": None,
                                "tool_status": "running",
                            }
                            subagent_children.setdefault(parent_tool_id, []).append(child_entry)

                            # Tool use from subagent - send as subagent_tool_use
                            yield {
                                "type": "subagent_tool_use",
                                "agent_id": parent_tool_id,
                                "name": block.name,
                                "id": tool_id,
                                "input": tool_input
                            }
                        else:
                            # Regular top-level tool — record in ordered turn events
                            turn_events.append({"kind": "tool_use", "payload": tool_entry})
                            yield {
                                "type": "tool_use",
                                "name": block.name,
                                "id": tool_id,
                                "input": tool_input
                            }

                    elif isinstance(block, ToolResultBlock):
                        # Extract screenshot (as a data URI) from MCP content blocks
                        # before truncation strips the image data for display.
                        screenshot = extract_mcp_image(block.content)
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

                        # Tag with parent_tool_id when this result belongs to a
                        # subagent child so persistence can skip top-level emission.
                        result_entry = {
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output,
                            # non-null parent = subagent child (see tool_use tag).
                            "parent_tool_id": parent_tool_id,
                            "uuid": asst_uuid,
                        }
                        tool_messages.append(result_entry)

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            # RC1: the CLI (2.1.195+) launches Agent/Task subagents
                            # ASYNC WITHOUT run_in_background in the tool input — the
                            # launch ToolResult here is only the async launch ACK, so
                            # the input-flag arm above never fired and the launch gate
                            # stayed empty → the drain pump suppressed every genuine
                            # agent completion as foreground. Arm the launch gate from
                            # the ACK sniff for a TOP-LEVEL launch whose result reads
                            # like the async-launch placeholder. A synchronous
                            # sub-agent's full result text does NOT match the sniffer,
                            # so it is never armed — preserving the #470/e83551e6
                            # foreground-suppression semantics.
                            _is_top_level_launch = (
                                tool_use_id
                                and not result_entry.get("parent_tool_id")
                            )
                            if (
                                _is_top_level_launch
                                and not task_info.get("run_in_background")
                                and _looks_like_subagent_launch_ack(output)
                            ):
                                task_info["run_in_background"] = True
                                task_registry.mark_background_launch(session_id, tool_use_id)
                                logger.info(
                                    "task-launch-classification session=%s tool_id=%s "
                                    "classification=background-ack",
                                    session_id, tool_use_id,
                                )
                            elif (
                                tool_use_id
                                and result_entry.get("parent_tool_id")
                                and not task_info.get("run_in_background")
                                and _looks_like_subagent_launch_ack(output)
                            ):
                                # Subagent-lane mirror of the ACK arm above: a
                                # grandchild Agent launched async records the
                                # DEDICATED subagent-lane mark (never the parent
                                # gate — no pin/chip), so its completion can
                                # still unpark instead of stranding the waiting
                                # sub-agent.
                                task_info["run_in_background"] = True
                                task_registry.mark_subagent_background_launch(session_id, tool_use_id)
                                logger.info(
                                    "task-launch-classification session=%s tool_id=%s "
                                    "classification=background-ack-subagent-lane",
                                    session_id, tool_use_id,
                                )
                            # A backgrounded sub-agent's FIRST ToolResult is only the
                            # launch ACK, not its real output — keep the node RUNNING
                            # (its inner activity streams via parent_tool_use_id and it
                            # completes on the terminal TaskNotificationMessage). Only a
                            # foreground Task/Agent's ToolResult is the final result.
                            if not (task_info.get("run_in_background") or _looks_like_subagent_launch_ack(output)):
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
                        # Check if this tool result is from a subagent.
                        elif is_subagent_lane:
                            # Merge result into the matching child entry under the parent subagent
                            children = subagent_children.get(parent_tool_id, [])
                            for child in children:
                                if child.get("tool_id") == tool_use_id:
                                    child["tool_result"] = output
                                    child["tool_status"] = (
                                        "error" if getattr(block, 'is_error', False) else "complete"
                                    )
                                    break

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
                            # Regular top-level tool_result — record in ordered events
                            turn_events.append({"kind": "tool_result", "payload": result_entry})
                            event = {
                                "type": "tool_result",
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": getattr(block, 'is_error', False)
                            }
                            # Include screenshot for browser tools (MCP content has image blocks)
                            if screenshot:
                                event["screenshot"] = screenshot
                            yield event

                    elif _SERVER_TOOL_TYPES and isinstance(block, _SERVER_TOOL_TYPES):
                        # Server-executed tool call. Treated like a top-level
                        # tool_use for persistence/UI, flagged `server_side` so
                        # the client can badge it (we never get a caller-side
                        # result to pair with).
                        tool_id = block.id
                        if tool_id:
                            tool_id_to_name[tool_id] = block.name
                        has_visible_content = True
                        tool_entry = {
                            "type": "tool_use",
                            "name": block.name,
                            "tool_id": tool_id,
                            "input": block.input,
                            "parent_tool_id": None,
                            "uuid": asst_uuid,
                            "server_side": True,
                        }
                        tool_messages.append(tool_entry)
                        turn_events.append({"kind": "tool_use", "payload": tool_entry})
                        yield {
                            "type": "tool_use",
                            "name": block.name,
                            "id": tool_id,
                            "input": block.input,
                            "server_side": True,
                        }

                    elif _SERVER_TOOL_RESULT_TYPES and isinstance(block, _SERVER_TOOL_RESULT_TYPES):
                        tool_use_id = getattr(block, 'tool_use_id', None)
                        tool_name = tool_id_to_name.get(tool_use_id, "server_tool")
                        output = truncate_large_payload(block.content)
                        has_visible_content = True
                        result_entry = {
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output,
                            "parent_tool_id": None,
                            "uuid": asst_uuid,
                            "server_side": True,
                        }
                        tool_messages.append(result_entry)
                        turn_events.append({"kind": "tool_result", "payload": result_entry})
                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_use_id": tool_use_id,
                            "output": output,
                            "is_error": False,
                            "server_side": True,
                        }

                metadata["model"] = message.model

                # Per-turn usage from AssistantMessage (SDK 0.1.49+)
                if message.usage:
                    # Track per-API-call values (overwrite → last call wins).
                    # Used by _persist_turn_results as the real context-window
                    # size; ResultMessage.usage is cumulative and unusable here.
                    metadata["last_call_tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["last_call_cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["last_call_cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)
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
                is_subagent_lane = parent_tool_id is not None
                # JSONL transcript uuid — shared by every block in this UserMessage.
                user_msg_uuid = getattr(message, "uuid", None)
                logger.debug(f"[WS] UserMessage received: content_type={type(message.content).__name__}, parent_tool_id={parent_tool_id}")

                # Suppress local command output from subagent UserMessages.
                is_subagent_user_msg = is_subagent_lane

                # Handle string content (rare but possible)
                if isinstance(message.content, str):
                    content = message.content
                    logger.debug(f"[WS] UserMessage string content preview: {content[:200] if content else 'empty'}")
                    # NOTE: a CLI-injected <task-notification> user-lane steering
                    # message can flow through here mid-turn. No latch is needed:
                    # the drain pump never delivers an app unpark for the
                    # backgrounded Task/Agent class at all (cli-owned-wake — see
                    # _drain_notifications_impl), so there is no duplicate to
                    # suppress at this source. PR #498's in-stream text latch was
                    # removed as proven-dead: at-rest injections never pass
                    # through stream_to_websocket (they are consumed by the pump
                    # between turns).
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
                        # NOTE: no straddle latch needed for a CLI-injected
                        # <task-notification> observed here — the pump never
                        # app-delivers the backgrounded Task/Agent class
                        # (cli-owned-wake), so nothing can duplicate. See the
                        # string-content branch above.
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

                        # Collect tool result for storage (tagged with parent_tool_id
                        # if it's a subagent child so persistence skips top-level emission)
                        result_entry = {
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_id": tool_use_id,
                            "output": output,
                            # non-null parent = subagent child (see tool_use tag).
                            "parent_tool_id": parent_tool_id,
                            "uuid": user_msg_uuid,
                        }
                        tool_messages.append(result_entry)

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            # RC1 (UserMessage lane): the CLI (2.1.195+) launches
                            # Agent/Task subagents ASYNC without run_in_background in
                            # the tool input — the launch ToolResult here is only the
                            # async launch ACK, so the input-flag arm never fired and
                            # the drain pump suppressed every genuine agent completion
                            # as foreground. Arm the launch gate from the ACK sniff for
                            # a TOP-LEVEL launch. A synchronous sub-agent's full result
                            # text does NOT match the sniffer, so it is never armed —
                            # preserving the #470/e83551e6 foreground semantics.
                            _is_top_level_launch = (
                                tool_use_id
                                and not result_entry.get("parent_tool_id")
                            )
                            if (
                                _is_top_level_launch
                                and not task_info.get("run_in_background")
                                and _looks_like_subagent_launch_ack(output)
                            ):
                                task_info["run_in_background"] = True
                                task_registry.mark_background_launch(session_id, tool_use_id)
                                logger.info(
                                    "task-launch-classification session=%s tool_id=%s "
                                    "classification=background-ack",
                                    session_id, tool_use_id,
                                )
                            elif (
                                tool_use_id
                                and result_entry.get("parent_tool_id")
                                and not task_info.get("run_in_background")
                                and _looks_like_subagent_launch_ack(output)
                            ):
                                # Subagent-lane mirror of the ACK arm above (see
                                # the AssistantMessage-lane sibling): dedicated
                                # mark only — no pin/chip via the parent gate.
                                task_info["run_in_background"] = True
                                task_registry.mark_subagent_background_launch(session_id, tool_use_id)
                                logger.info(
                                    "task-launch-classification session=%s tool_id=%s "
                                    "classification=background-ack-subagent-lane",
                                    session_id, tool_use_id,
                                )
                            # Backgrounded sub-agent: this ToolResult is only the launch
                            # ACK ("Async agent launched successfully …"), not the real
                            # result. Keep the node RUNNING — its inner activity streams
                            # via parent_tool_use_id (in-stream and, after the parent
                            # parks, via the drain pump) and it completes on the terminal
                            # TaskNotificationMessage. Do NOT emit subagent_done / delete
                            # tracking, or the node renders "completed / no activity".
                            if not (task_info.get("run_in_background") or _looks_like_subagent_launch_ack(output)):
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
                        # Check if this tool result is from a subagent.
                        elif is_subagent_lane:
                            # Merge result into the matching child entry under the parent subagent
                            children = subagent_children.get(parent_tool_id, [])
                            for child in children:
                                if child.get("tool_id") == tool_use_id:
                                    child["tool_result"] = output
                                    child["tool_status"] = "error" if block.is_error else "complete"
                                    break
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
                            # Regular top-level tool_result — record in ordered events
                            turn_events.append({"kind": "tool_result", "payload": result_entry})
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
                _rli = message.rate_limit_info
                # Yield the frame to clients on THIS install's WS. Cross-install
                # views (peers, offloaded/cloud-relayed sessions) do NOT receive
                # this frame — the relay serves live_session_store, which never
                # carries rate_limit. They see the change only via the shared
                # usage_snapshots row + cluster broadcast we persist below.
                yield _rate_limit_event_dict(_rli, session_id)
                await _apply_rate_limit_live_event(_rli)

            elif isinstance(message, HookEventMessage):
                logger.debug(f"[WS] Hook event: {getattr(message, 'hook_event_name', None)}")
                yield {
                    "type": "hook_event",
                    "hook_event_name": getattr(message, "hook_event_name", None),
                    "matcher": getattr(message, "matcher", None),
                    "decision": getattr(message, "decision", None),
                    "reason": getattr(message, "reason", None),
                    "system_message": getattr(message, "system_message", None),
                    "tool_name": getattr(message, "tool_name", None),
                    "session_id": session_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
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
                # Surface HTTP error status code (SDK 0.1.81+); emit before result frame
                api_err = getattr(message, "api_error_status", None)
                if api_err is not None:
                    metadata["api_error_status"] = api_err
                    yield {"type": "api_error", "status": api_err, "session_id": session_id}
                    # Funnel through the unified notification system (persist + the
                    # bell + all the user's devices). The frontend `api_error`
                    # handler no longer toasts — the `notification` event does.
                    await _emit_api_error_notification(api_user_id, session_id, api_err)
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

        # Finalize any trailing thinking block
        if _current_thinking:
            thinking_entry = {"content": "".join(_current_thinking)}
            thinking_blocks.append(thinking_entry)
            turn_events.append({"kind": "thinking", "payload": thinking_entry})
            _current_thinking.clear()

        # Final drain of stderr queue after streaming loop completes
        for stderr_msg in await drain_stderr_queue():
            if stderr_msg.get("type") == "plugin_message":
                plugin_messages_acc.append(stderr_msg)
                turn_events.append({"kind": "plugin", "payload": stderr_msg})
            yield stderr_msg

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"[WS] Query cancelled for session {session_id}")
        # Don't re-raise - we handle it gracefully by yielding interrupted message

    except Exception as e:
        logger.error(f"[WS] Query error for session {session_id}: {e}")
        # Surface the subprocess's own stderr (the exit code / stack trace it
        # printed before dying). This is the interactive path that owns
        # background sub-agents; when its subprocess exits mid-stream those
        # tasks get reaped and vanish, so recording WHY here is the key signal.
        log_captured_cli_stderr(session_id, state.cli_stderr_lines, context="ws mid-stream")
        state.is_connected = False
        crashed = True
        yield {"type": "error", "message": str(e)}
        # Do NOT early-return here. Fall through to `finally` and the
        # _persist_turn_results call below so the partial turn AND the
        # sdk_session_id link are written even on a mid-turn crash. The bare
        # `return` that used to live here skipped all persistence — that is how
        # OOM/SIGKILL crashes orphaned sessions (sdk_session_id stayed NULL →
        # blank history). The `crashed` flag suppresses the trailing `done`
        # frame since we've already emitted an `error`.

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
    # turn_events provides the stream-ordered sequence so rows are inserted
    # in the same order the events arrived (fixes visual flow on reload).
    # subagent_children embeds subagent internal tool usage into the parent's
    # agent_children metadata so the UI can rebuild nested displays on reload.
    await _persist_turn_results(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        tool_messages=tool_messages,
        response_text=response_text,
        metadata=metadata,
        profile_id=profile_id,
        interrupted=interrupted,
        log_prefix="[WS] ",
        thinking_blocks=thinking_blocks,
        system_messages=system_messages_acc,
        plugin_messages=plugin_messages_acc,
        turn_events=turn_events,
        subagent_children=subagent_children,
        prompt=prompt,
        working_dir=getattr(options, "cwd", None),
    )

    # Push any agent-memory the CLI wrote this turn up to the shared DB.
    await push_memory(project_id, getattr(options, "cwd", None))

    # For slash commands that produced no visible content, yield a notification
    # SDK built-in commands like /memory, /cost, /model execute inside the subprocess
    # and may not produce any streaming events visible to the frontend
    if is_slash_command and not has_visible_content and not interrupted and not crashed:
        cmd_name = prompt.strip().split()[0]  # e.g., "/memory"
        logger.info(f"[WS] Slash command {cmd_name} produced no visible output for session {session_id}")
        yield {
            "type": "system",
            "subtype": "local_command",
            "data": {"content": f"Command {cmd_name} completed (no output)."}
        }

    # Yield done or interrupted event. On a crash we've already emitted an
    # `error` frame above; emitting `done` too would mislead the frontend into
    # treating the turn as a clean completion, so skip the terminal frame
    # entirely (persistence has already run).
    if crashed:
        pass
    elif interrupted:
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

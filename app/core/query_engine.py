"""
Query engine for executing Claude queries with profiles

- Keep the session's pooled adapter connected for the lifetime of a session
- Don't disconnect after every query (causes async context issues)
- Create a new adapter with the 'resume' option when resuming after app restart

This module is the main orchestrator. Related logic is split across:
- query_options.py        — option-building helpers
- query_streaming.py      — streaming/truncation utilities
- query_persistence.py    — batched DB write logic and error extraction
- query_background.py     — background (fire-and-forget) query execution
- query_session_lifecycle.py — session state inspection, interrupt, active lists

A few of their symbols are re-exported here for callers and test patch
targets that still reach them through this module.
"""

import json
import logging
import os
import re
import time
import uuid
import asyncio
from typing import Optional, Dict, Any, AsyncGenerator, List
from datetime import datetime, timezone

# Provider-agnostic mirrors of the SDK streaming message vocabulary (identical
# class/attribute names — see app/core/provider/events.py). Every stream this
# module consumes is translated at its boundary (adapter.receive_events()), so
# the dispatch below never sees a claude_agent_sdk message class. Version skew
# is absorbed inside the translation layer.
from app.core.provider.events import (
    AssistantMessage, UserMessage, TextBlock, ToolUseBlock,
    ToolResultBlock, ResultMessage, SystemMessage, StreamEvent,
    ServerToolUseBlock, ServerToolResultBlock,
    TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage,
    TaskUpdatedMessage, RateLimitEvent, HookEventMessage,
    SteerDeliveredEvent,
)
_SERVER_TOOL_TYPES: tuple = (ServerToolUseBlock,)
_SERVER_TOOL_RESULT_TYPES: tuple = (ServerToolResultBlock,)
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from fluid.engine.delegated import ClaudeCliMissingError

from app.db import database
from app.db import turns as turns_db
from app.db.async_database import run_sync
from pathlib import Path
from app.core.config import settings
from app.core.session_pool import (
    SessionState, get_session_pool, is_subprocess_alive,
    requires_reconnect, reconnect_option_names, mark_subprocess_oom_killable,
    subprocess_pid,
)
from app.core.session_title import derive_session_title
from app.core.profiles import get_profile
from app.core.sync_engine import sync_engine
from app.core.permission_handler import PermissionDecision, permission_handler, _DeferSentinel
from app.core.user_question_handler import user_question_handler

from app.core.task_registry import task_registry
from app.core.system_message_filter import HIDDEN_SYSTEM_SUBTYPES

# Helpers from the extracted modules. Some are also reached through this module
# by other code and tests (``qe.<name>`` patch targets, app/api imports).
from app.core.query_streaming import (  # noqa: F401 — re-exports
    extract_mcp_image,
    is_context_snapshot_only as _is_context_snapshot_only,
    truncate_large_payload,
)
from app.core.query_options import (  # noqa: F401 — re-exports
    resolve_note_references,
    DEFAULT_SECURITY_INSTRUCTIONS,
    build_options_from_profile,
    WorktreeUnavailableError,
)
from app.core.query_persistence import (  # noqa: F401 — re-exports
    _wrap_stderr_capture,
    _extract_connection_error,
    _persist_turn_results,
    USAGE_PROVIDER_KEY,
    capture_usage_identity,
    schedule_context_usage_capture,
    log_captured_cli_stderr,
)
from app.core.session_context import set_current_session_id
from app.core.memory_sync import (
    memory_leg_for_options, reconcile_memory, push_memory,
)
from app.core.query_background import (  # noqa: F401 — re-exports
    start_background_query,
)
from app.core.query_session_lifecycle import (  # noqa: F401 — re-exports
    interrupt_session,
    stop_session_task,
    get_streaming_sessions,
)

logger = logging.getLogger(__name__)

# Gate for verbose SDK-frame-ordering tracing in the streaming loop. Off unless
# either env var is truthy. Read once at import so the hot path only does a bool
# check. (Mirrors the STREAM_DEBUG/AIHUB_DEBUG_STREAM gate used elsewhere.)
_STREAM_DEBUG = bool(
    os.environ.get("STREAM_DEBUG") or os.environ.get("AIHUB_DEBUG_STREAM")
)

_LEGACY_NOTE_REF = re.compile(r"@Notes/(note-[a-f0-9]+)")


def _resolve_note_references_for(prompt: str, api_user_id: Optional[str]) -> str:
    """Expand legacy ``@Notes/note-<id>`` references the caller may read.

    Same rule as the notes API (``app.api.notes._owned_note``): the owner (no
    key) reaches every note; an API key only the notes it wrote
    (``notes.api_user_id``). A reference to any other note is left as typed,
    like one to a note that does not exist, so a key cannot pull the owner's
    note into its session by id.
    """
    if not api_user_id:
        return resolve_note_references(prompt)

    def expand(match: "re.Match[str]") -> str:
        note = database.get_note(match.group(1))
        if not note or note.get("api_user_id") != api_user_id:
            return match.group(0)
        return resolve_note_references(match.group(0))

    return _LEGACY_NOTE_REF.sub(expand, prompt)


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
        # Overage fields (None when absent)
        "overage_status": getattr(rli, "overage_status", None),
        "overage_resets_at": getattr(rli, "overage_resets_at", None),
        "overage_disabled_reason": getattr(rli, "overage_disabled_reason", None),
        "session_id": session_id,
        "instance_id": settings.effective_instance_id,
    }


async def _apply_rate_limit_live_event(rli: Any) -> None:
    """Persist a live ``rate_limit`` refinement into the shared usage snapshot.

    A yielded ``rate_limit`` frame reaches only clients connected directly to
    THIS install's WS; peers (and offloaded/cloud-relayed views) see the change
    only through the shared ``usage_snapshots`` row persisted here plus the
    cluster ``usage_snapshot`` broadcast. Best-effort — never break streaming.
    """
    try:
        from app.core.usage_limits import usage_limits_monitor
        await usage_limits_monitor.apply_live_event_and_broadcast(
            rli.rate_limit_type, rli.utilization, rli.resets_at,
            status=getattr(rli, "status", None),
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
# and map them onto the registry vocabulary. "stopped" is included defensively
# even though TaskUpdatedStatus uses "killed" for the TaskStop case.
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
            #   - it DOES park its parent turn: the parent's receive_events()
            #     returns at the parent ResultMessage while the shell keeps running,
            #     and its completion arrives LATER as a terminal
            #     TaskNotificationMessage on the SAME pooled adapter — exactly
            #     like a background sub-agent. mark_sdk_background_task pins the
            #     parked session in the pool (else idle eviction kills the pooled
            #     client and drops the completion → frontend never unparks).
            #     CLI shells are tracked SEPARATELY from the app's own Monitor-tool
            #     shells (mcp/builtin_tools.py), which are app-managed, independent
            #     of the pooled client, and must NOT pin.
            # A FOREGROUND shell must arm NEITHER mark: its result returns inline as
            # a normal tool_result this turn, so treating it as a background launch
            # would let its at-rest notification pass the unpark gates and fire a
            # stale "<task-notification>" follow-up turn after the turn ended,
            # re-telling the model about commands it already ran and reported. It
            # still gets a registry entry above for the TaskList/live SHELL badge;
            # complete_task below clears it when the command finishes.
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
                    # Without this the grandchild's terminal notification would
                    # be suppressed as foreground and a sub-agent parked awaiting
                    # it would strand forever.
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
                # work (that would fire a stale follow-up turn).
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
            # leaking from the pooled adapter's buffer and must be suppressed.
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
# would be invisible to every launch gate: its terminal notification would be
# suppressed as foreground and the Monitor-parked sub-agent would strand
# forever. The CLI enqueues the <task-notification> into the parked parent's
# queue and never self-dequeues a SUBAGENT-owned one — unlike TOP-LEVEL Monitors, whose wakes
# the CLI owns end-to-end (enqueue + self-dequeue) and which therefore must
# keep being suppressed by the app. So this name sniff applies to SIDECHAIN
# tool_uses only (the streaming loop gates it on parent_tool_use_id).
_IMPLICIT_BACKGROUND_TOOL_NAMES = ("Monitor",)


# Phrases that identify a backgrounded sub-agent's launch-ACK ToolResult — the
# immediate "Async agent launched successfully … working in the background …"
# placeholder the CLI returns the moment a Task is started with
# ``run_in_background``. This is a fallback to the ``run_in_background`` flag (the
# primary signal) for the rare case the flag is absent; mirrors the content sniff
# in ``jsonl_parser``.
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


def _task_launch_failure_error(output: Any, is_error: bool) -> Optional[str]:
    """Did this Task ToolResult report a launch that DIED before spawning a worker?

    The fluid Task dispatcher (``app/core/task_dispatch.py`` ``dispatch_task_tool``)
    returns ``{"ok": false, "error": ...}`` when it rejects a launch before any
    worker exists (e.g. a model outside the provider's curated list). That — like
    an ``is_error`` ToolResult — is POSITIVE evidence the launch is dead: no
    TaskStarted and no terminal notification will ever follow. So the card must
    settle to error right here at result time instead of deferring to the
    end-of-turn flush, which an interrupt/restart mid-turn skips entirely,
    leaving the card spinning forever on live AND on every replay.

    Returns the error text to surface on the card, or None when this result is
    not a launch failure.
    """
    text = output if isinstance(output, str) else str(output or "")
    if is_error:
        return text.strip() or "Task launch failed."
    s = text.strip()
    if not s.startswith("{"):
        return None
    try:
        payload = json.loads(s)
    except Exception:
        return None
    if isinstance(payload, dict) and payload.get("ok") is False:
        return str(payload.get("error") or s)
    return None


def _should_defer_subagent_done(
    session_id: Optional[str],
    tool_use_id: Optional[str],
    task_info: Dict[str, Any],
    output: Any,
) -> str:
    """Classify a Task/Agent launch ToolResult → ``"suppress"``|``"defer"``|``"emit"``.

    If the phrase sniffer were the only thing stopping a background-launch ACK
    from firing a premature ``subagent_done``, a reworded CLI ACK would fail both
    the ``run_in_background`` arming AND the suppression together — persisting a
    false terminal completion at launch. This classifier is a
    defer-don't-terminate policy that writes a terminal event ONLY on positive
    evidence of foreground, and DEFERS every ambiguous case. Priority:

    (a) ``suppress`` — the launch is explicitly backgrounded: the ``run_in_background``
        input flag, the cross-turn seed, or one of the in-stream ACK arms above
        already pinned ``task_info["run_in_background"]``.
    (b) ``suppress`` — registry evidence the launch is a running background task:
        ``was_background_launch`` / ``was_subagent_background_launch`` (armed this
        or a prior turn) OR the reverse lookup
        ``running_background_task_for_tool_use`` (a cross-turn launch still in
        flight). When this fires, pin the turn-local flag AND
        ``mark_background_launch`` so every later frame this turn agrees.
    (c) ``suppress`` — the phrase sniffer still recognises the ACK text (belt: the
        in-stream ACK arms already fired for a matching top-level/sub-agent
        launch; this covers the residual shapes). Sniffer kept EXACTLY as-is.
    (d) ``defer`` — no evidence either way: park the entry, keep the node RUNNING,
        emit nothing. A genuine synchronous foreground launch settles at the
        end-of-turn drain flush; a background launch whose ACK was reworded
        settles on its eventual terminal TaskNotificationMessage.
    """
    # (a) explicit background flag / cross-turn seed / already-armed ACK.
    if task_info.get("run_in_background"):
        return "suppress"
    # (b) registry evidence — arm turn-local + durable gate for consistency.
    if (
        session_id
        and tool_use_id
        and (
            task_registry.was_background_launch(session_id, tool_use_id)
            or task_registry.was_subagent_background_launch(session_id, tool_use_id)
            or task_registry.running_background_task_for_tool_use(session_id, tool_use_id)
        )
    ):
        task_info["run_in_background"] = True
        task_registry.mark_background_launch(session_id, tool_use_id)
        return "suppress"
    # (c) phrase-sniffer fallback (unchanged).
    if _looks_like_subagent_launch_ack(output):
        return "suppress"
    # (d) ambiguous — DEFER.
    return "defer"


def _read_task_output_file(output_file: str, max_chars: int = 60_000) -> str:
    """Read a completed background task's full result off the local disk.

    A ``TaskNotificationMessage`` carries only a ``summary`` plus a path
    to ``output_file`` on the local host; the FULL sub-agent result lives in that
    file and there is no provider helper to read it. This is the only way to obtain it.

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
        title = derive_session_title(
            prompt, max_chars=50, word_boundary=False
        )
        session = await run_sync(
            database.create_session,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id,
            automation_id=automation_id,
            # Automation-created sessions (automation_id set) run headless and
            # must NOT land in the user's Active group — only interactive,
            # user-initiated sessions are "active".
            is_active=automation_id is None,
        )
        resume_id = None

        # Notify all connected frontends so the sidebar updates
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_user_session_created(
            user_id="admin",
            session_id=session_id,
            source_device_id=None
        )

    turn_started_at = datetime.now(timezone.utc)

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

    # Build options with user-scoped credential resolution.
    # A WorktreeUnavailableError propagates here (non-streaming path) just like
    # the ValueErrors above — the caller turns it into an error response rather
    # than silently running the worktree session in the main checkout.
    # Build options + wire plugin/DB hooks (incl. blocking PreToolUse guards)
    # through the shared sdk_options seam, the same as the WebSocket path.
    from app.core.sdk_options import build_session_options
    from app.core.query_options import ensure_pending_checkout
    # Async wait for an in-flight ensure_worktree. Must stay on the event
    # loop: a sync poll would occupy a run_sync worker for 30s.
    if session:
        await ensure_pending_checkout(session)
    # Off the event loop: build_options_from_profile is synchronous and, for a
    # branch session, materializes the checkout (git fetch + git worktree add
    # via subprocess.run, up to 90s) plus several DB reads. Running it inline
    # blocked every other coroutine in the process for the whole spawn.
    options, agents_dict = await asyncio.to_thread(
        build_session_options,
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id,
        api_user_id=api_user_id,
        session_id=session_id,
        hook_project_id=project_id,
    )

    # Credential gate (see _stream_to_websocket_impl): refuse with the setup
    # message rather than spawn a CLI that has no credential.
    from app.core import credential_gate

    _refusal = await credential_gate.refusal_for_options_async(options)
    if _refusal:
        logger.warning(f"[Query] Turn refused for session {session_id}: {_refusal}")
        raise credential_gate.AgentSetupRequired(_refusal)

    # Resolve virtual @Notes/note-id references to actual content
    prompt = _resolve_note_references_for(prompt, api_user_id)

    # Execute query and collect response
    response_text = []
    metadata = {
        "usage_settlement_key": await capture_usage_identity(session_id),
        USAGE_PROVIDER_KEY: getattr(options, "aih_provider_id", None),
    }
    sdk_session_id = None
    sdk_link_persisted = False

    # Pull shared agent-memory from the DB before the CLI reads it.
    await reconcile_memory(
        project_id, getattr(options, "cwd", None), memory_leg_for_options(options)
    )

    # Backend selection goes through the provider seam (see
    # app/core/provider/factory.py); the fluid HarnessAdapter is the only
    # backend. Routing through make_adapter() keeps this headless path on the
    # same backend as every other spawn path, so a wake turn (idle watchdog,
    # ScheduleWakeup, automations, connectors, POST /api/v1/query) resumes its
    # fluid session id (ses_...) with full context.
    #
    # One-shot lifecycle: deliberately NOT registered in the session pool (no
    # pooled reuse for headless turns) — the adapter is torn down in the
    # finally below. The prompt is handed over as a plain string via
    # send_query(), like the background path does.
    from app.core.provider.factory import make_adapter
    adapter = make_adapter()

    try:
        await adapter.connect_with_sdk_options(options)
        await adapter.send_query(prompt)

        async for message in adapter.receive_events():
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
                    if not sdk_link_persisted:
                        try:
                            # launched_model is persisted on EVERY spawn/init,
                            # including fresh-subprocess resumes — the host-op
                            # guard reads sessions.launched_model with precedence
                            # over transcript msg_model, so a resume that relaunches
                            # with a different effective model must re-persist or the
                            # guard shadows newer truth with a stale value.
                            # The sdk_session_id link stays resume-gated: a resume
                            # keeps the sdk_session_id it was launched with.
                            update_kwargs = {
                                "launched_model": getattr(options, "model", None),
                            }
                            if resume_id is None:
                                update_kwargs["sdk_session_id"] = sdk_session_id
                            await run_sync(
                                database.update_session,
                                session_id,
                                **update_kwargs,
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
                metadata["model"] = message.model

                # Track per-API-call usage so we can persist the LAST call's
                # values (= current context window size). ResultMessage.usage
                # is cumulative across the agent loop and would inflate.
                #
                # Subagent (sidechain) messages carry parent_tool_use_id and run
                # their own, separate context window — counting them here would
                # overwrite the parent session's last-call figures with an
                # unrelated number, so they are skipped entirely.
                if getattr(message, "usage", None) and not getattr(message, "parent_tool_use_id", None):
                    # A snapshot-only record (delegated leg) reports a context
                    # breakdown for a call whose tokens were already counted by
                    # an earlier record; letting its zeroes land here would
                    # blank the persisted context-window size.
                    if not _is_context_snapshot_only(message.usage):
                        metadata["last_call_tokens_in"] = message.usage.get("input_tokens", 0)
                        metadata["last_call_cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                        metadata["last_call_cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)

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
                # Terminal reason + typed per-model usage pass-through
                metadata["terminal_reason"] = getattr(message, "terminal_reason", None)
                metadata["model_usage"] = getattr(message, "model_usage", None)
                # Surface provider-reported errors
                result_errors = getattr(message, 'errors', None)
                if result_errors:
                    metadata["errors"] = result_errors
                    logger.warning(f"ResultMessage contains errors: {result_errors}")
                # Surface HTTP error status code
                api_err = getattr(message, "api_error_status", None)
                if api_err is not None:
                    metadata["api_error_status"] = api_err
                    # Funnel through the unified notification system so the
                    # non-streaming /query and /conversation paths reach the
                    # bell + every device, matching the streaming paths.
                    await _emit_api_error_notification(
                        api_user_id, session_id, api_err,
                        delegated=_adapter_is_delegated(adapter),
                    )
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)
                    # Any call this turn with harness-estimated token counts
                    # (the adapter stamps the terminal usage); booked as a
                    # flag on the ledger row, never subtracted.
                    metadata["usage_estimated"] = bool(message.usage.get("usage_estimated"))

    except (Exception, BaseExceptionGroup) as e:
        # Unwrap ExceptionGroup from an anyio.TaskGroup (Python 3.11+)
        if isinstance(e, BaseExceptionGroup) and e.exceptions:
            actual = e.exceptions[0]
            logger.error(f"Query execution error (unwrapped from ExceptionGroup): {actual}", exc_info=actual)
            raise actual from e
        logger.error(f"Query execution error: {e}")
        raise
    finally:
        # One-shot path owns the adapter outright (never pooled), so tear the
        # subprocess/harness session down here — including on the crash path.
        try:
            await adapter.disconnect()
        except Exception as disconnect_err:
            logger.warning(
                f"execute_query adapter disconnect failed for "
                f"{session_id}: {disconnect_err}"
            )

    # Persist session data to database in a batched write.
    await _persist_turn_results(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        metadata=metadata,
        profile_id=profile_id,
    )

    # Terminal history row for a turn that ran outside the scheduler, so its
    # provider spend is attributable. Written AFTER the fact and never live —
    # see turns.record_adhoc_turn for why a queued/running row here would be
    # double-run or reaped. The exception paths above raise before this point
    # and deliberately record nothing (they write no usage_log row either).
    try:
        _is_error = bool(metadata.get("is_error"))
        await run_sync(
            turns_db.record_adhoc_turn,
            session_id,
            "automation_step" if automation_id else "api_query",
            outcome="aborted" if _is_error else "completed",
            prompt=prompt,
            profile_id=profile_id,
            project_id=project_id,
            stop_reason=metadata.get("stop_reason"),
            error=str(metadata["errors"]) if (_is_error and metadata.get("errors")) else None,
            started_at=turn_started_at,
            # Context trail for scheduler-enqueued successors (unpark wake,
            # auto-continue): they inherit this turn's overrides and api_user
            # attribution via turn_scheduler._enqueue_context_wake.
            overrides=overrides or None,
            payload={"api_user_id": api_user_id} if api_user_id else None,
        )
    except Exception as e:
        logger.warning(f"Failed to record ad-hoc turn for {session_id}: {e}")

    # Push agent-memory the CLI wrote this turn up to the shared DB.
    await push_memory(
        project_id, getattr(options, "cwd", None), memory_leg_for_options(options)
    )

    # Notify connected frontends that this session has new messages
    # (non-streaming queries render nothing live, so we broadcast a
    # session_updated event so the frontend poller picks up the changes)
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


def _adapter_is_delegated(adapter: Any) -> bool:
    """Whether ``adapter`` runs the delegated leg (the claude CLI)."""
    return getattr(adapter, "provider_kind", None) == "delegated"


async def _emit_api_error_notification(
    api_user_id: Optional[str],
    session_id: Optional[str],
    api_err: Any,
    *,
    delegated: bool = False,
) -> None:
    """Funnel a provider ``api_error`` through the unified notification system.

    Single source of truth for API-level errors (Anthropic 429 / 5xx): persists
    to the bell and broadcasts the user-scoped ``notification`` event so every
    device the user has open renders one consistent notification. The interactive
    "Fix with AI" button is rebuilt client-side (keyed on ``category`` —
    closures can't cross the wire), so we only ship a serializable action
    descriptor here. Never raises — must not break the streaming path.

    ``delegated`` says the turn ran through the claude CLI. A 401 there means
    the CLI's own sign-in is missing or expired, which only signing in again
    fixes, so the notification says how instead of the generic HTTP text.
    """
    try:
        from app.services.notification_service import emit_notification

        try:
            status = int(api_err)
        except (TypeError, ValueError):
            status = 0

        if status == 401 and delegated:
            from app.core.claude_relogin_notice import LOGIN_COMMAND

            await emit_notification(
                api_user_id or "admin",
                severity="error",
                message=(
                    "Claude Code refused the request (HTTP 401): its sign-in on this "
                    "worker is missing or has expired. "
                    f"Run `{LOGIN_COMMAND}` on the worker, or use Settings > "
                    "Providers > Claude to sign in again, then retry."
                ),
                category="api_error",
                surface="notification",
                session_id=session_id,
                persist=True,
            )
            return
        if status == 429:
            # Rate-limit (429) notifications are owned by ``account_failover``,
            # which surfaces the "rate-limited — wait or sign into a different
            # account" state on every 429 turn through its ``attempt_failover``
            # seam (native auto-switch was retired — OC-1/OC-4). Suppressing the
            # duplicate generic notification here keeps exactly one notification per
            # 429 turn: every 429 that reaches this funnel is on a path that
            # also runs ``attempt_failover`` (the streaming SSE wrapper,
            # ``turn_scheduler``, or ``execute_query_with_failover``).
            return
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
            surface="notification",
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
    """Stream a query through the session's pooled provider adapter.

    The adapter is reused across turns (disconnecting after each query causes
    async context issues); a new one is connected with ``resume`` when none is
    pooled or the options require a reconnect.
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
        title = derive_session_title(
            prompt, max_chars=50, word_boundary=False
        )
        session = await run_sync(
            database.create_session,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            title=title,
            api_user_id=api_user_id,
            # stream_query is the interactive SSE path only (called from
            # app/api/query.py); it has no automation_id and is never reached
            # from automation, so every session it creates is user-active.
            is_active=True,
        )
        _is_new_session = True  # Flag available for future analytics/logging
        logger.debug(f"Created new session {session_id} with title: {title}")

    # Store user message and broadcast to other devices. Skipped on a failover
    # retry (persist_user_message=False): the original entry already added the
    # user message to the DB and broadcast it — re-doing it here
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

    # Build options with user-scoped credential resolution.
    # WorktreeUnavailableError → refuse to run (never silently in main).
    from app.core.sdk_options import build_session_options
    from app.core.query_options import ensure_pending_checkout
    # Async wait for an in-flight ensure_worktree. Must stay on the event
    # loop: a sync poll would occupy a run_sync worker for 30s.
    if session:
        await ensure_pending_checkout(session)
    try:
        # Off the event loop — see execute_query: the build can run git
        # fetch/worktree add and several DB reads synchronously.
        options, agents_dict = await asyncio.to_thread(
            build_session_options,
            profile=profile,
            project=project,
            overrides=overrides,
            resume_session_id=resume_id,
            api_user_id=api_user_id,
            session_id=session_id,
        )
    except (WorktreeUnavailableError, ValueError) as e:
        # Worktree unrecoverable, or the resolved working directory doesn't
        # exist (e.g. a project path from another install) — surface a clear
        # error instead of an opaque subprocess spawn failure.
        logger.error(f"[Query] Cannot build session options for {session_id}: {e}")
        yield {
            "type": "error", "message": str(e),
            **({"code": "checkout_unavailable"} if isinstance(e, WorktreeUnavailableError) else {}),
        }
        return

    # Credential gate (see _stream_to_websocket_impl).
    from app.core import credential_gate

    _refusal = await credential_gate.refusal_for_options_async(options)
    if _refusal:
        logger.warning(f"[Query] Turn refused for session {session_id}: {_refusal}")
        yield credential_gate.refusal_event(_refusal)
        return

    # Wire plugin + DB hooks (incl. blocking PreToolUse guards) into this
    # spawn path, the same as the WebSocket path.
    # Kept outside the build try/except to preserve exception scoping.
    from app.core.hook_service import attach_hooks_to_options
    attach_hooks_to_options(options, project_id=project_id)

    # Yield init event
    yield {"type": "init", "session_id": session_id}

    turn_started_at = datetime.now(timezone.utc)

    # --- Session Pool: Acquire or create the pooled adapter ---
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
                # Runtime-changeable options (model, permission_mode) are baked
                # into the CLI subprocess at spawn. requires_reconnect() lets them
                # change without a respawn, so a mid-session switch must be pushed
                # into the live subprocess here (this path has no can_use_tool
                # compensation at all). Never raise: an exception between the
                # is_streaming claim and the main try/finally would strand the
                # flag and brick the pooled client.
                # The adapter writes each applied model / permission-mode change
                # back to these options (HarnessAdapter._record_applied), so they
                # hold the live values, not the connect-time ones.
                old_opts = state.connect_options
                if old_opts:
                    new_model = getattr(options, 'model', None)
                    old_model = getattr(old_opts, 'model', None)
                    # Same-provider-only hot-swap — see the WS path's note.
                    _same_provider = getattr(old_opts, 'aih_provider_id', None) == getattr(options, 'aih_provider_id', None)
                    if new_model and new_model != old_model and _same_provider:
                        try:
                            # Live hot-swap stays within the session's current provider;
                            # a provider change is caught by requires_reconnect() and
                            # forces a respawn instead of reaching this set_model() call.
                            await state.adapter.set_model(new_model)
                            logger.debug(f"Updated model to {new_model} for reused session {session_id}")
                            # Persist the switched model so launched_model never
                            # goes stale — the host-op guard reads this column.
                            try:
                                await run_sync(database.update_session, session_id, launched_model=new_model)
                            except Exception:
                                pass
                        except Exception as e:
                            logger.warning(f"Failed to set model on reused session {session_id}: {e}")

                    new_perm_mode = getattr(options, 'permission_mode', None)
                    old_perm_mode = getattr(old_opts, 'permission_mode', None)
                    if new_perm_mode and new_perm_mode != old_perm_mode:
                        try:
                            await state.adapter.set_permission_mode(new_perm_mode)
                            logger.debug(f"Updated permission_mode to {new_perm_mode} for reused session {session_id}")
                        except Exception as e:
                            logger.warning(f"Failed to set permission_mode on reused session {session_id}: {e}")

                logger.info(f"Reusing pooled client for session {session_id}")
            except BaseException:
                state.is_streaming = False
                state.streaming_started_at = None
                raise
        else:
            logger.info("Options changed for session %s (%s), creating fresh client",
                        session_id, ",".join(reconnect_option_names(existing_state, options)))
            await pool.remove(session_id, preserve_session=True)
            existing_state = None
    elif existing_state:
        logger.warning(f"Subprocess dead for pooled session {session_id}, creating fresh client")
        await pool.remove_dead(session_id)
        existing_state = None

    if not reusing_client:
        # Admission backpressure: refuse a NEW session if the box is at its
        # process/RAM/pool ceiling, rather than killing in-flight work later.
        # psutil process-tree walk — off the loop.
        # Lane-aware: the pool's subprocess cap counts DELEGATED sessions
        # only, and deciding the leg can read the provider row, so both
        # run in the same worker thread.
        incoming_lane = await asyncio.to_thread(memory_leg_for_options, options)
        refusal = await asyncio.to_thread(pool.admission_check, incoming_lane)
        if refusal is not None:
            logger.warning(f"[Admission] Refusing new session {session_id}: {refusal}")
            yield {"type": "error", "message": f"Server is at capacity — {refusal}. Please retry shortly."}
            return

        # Evict LRU if pool is full
        await pool.evict_if_needed(incoming_lane)

        logger.debug(f"Creating fluid harness adapter for session {session_id} (resume={resume_id is not None})")

        # Attribute this session to any task created inside adapter.connect()
        # below: it inherits this context, so an error such a task logs (e.g.
        # "Fatal error in message reader") carries the session id into error_log
        # (see error_log_handler.emit).
        set_current_session_id(session_id)

        # Wrap stderr callback to collect lines for error reporting
        options.stderr, cli_stderr_lines = _wrap_stderr_capture(options.stderr)

        # Pull shared agent-memory from the DB before the CLI reads it.
        await reconcile_memory(
            project_id, getattr(options, "cwd", None), memory_leg_for_options(options)
        )

        # Backend selection via the provider seam, same as the WS path below:
        # make_adapter() always returns the fluid HarnessAdapter.
        from app.core.provider.factory import make_adapter
        adapter = make_adapter()

        try:
            # Timeout prevents hanging if MCP servers are unreachable
            await asyncio.wait_for(adapter.connect_with_sdk_options(options), timeout=60.0)
            logger.debug(f"Connected to Claude SDK for session {session_id}")
        except asyncio.TimeoutError:
            logger.error(f"Timed out connecting to Claude SDK for session {session_id} (60s)")
            yield {"type": "error", "message": "Connection timed out — an MCP server may be unreachable"}
            return
        except ClaudeCliMissingError as e:
            # Setup problem, not a crash: no traceback, the message is the fix.
            logger.warning(f"Claude CLI missing for session {session_id}: {e}")
            yield {"type": "error", "message": str(e)}
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
            cli_stderr_lines=cli_stderr_lines,
        )
        pool.put(session_id, state)

    # Mark as streaming
    state.is_streaming = True
    state.streaming_started_at = datetime.now(timezone.utc)
    state.last_activity = datetime.now(timezone.utc)

    # Claim-to-try guard: is_streaming is claimed but the protective
    # try/finally hasn't started yet — any exit here (the sync-log
    # await can raise or be cancelled) must clear the flag or the pooled
    # client is bricked (busy error forever, never idle-evicted).
    try:
        # Generate a message ID for streaming sync
        assistant_msg_id = f"streaming-{session_id}-{datetime.now(timezone.utc).timestamp()}"

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
        tool_id_to_name = {}  # Track tool_use_id → tool_name for ToolResultBlock lookup
        metadata = {
            "usage_settlement_key": await capture_usage_identity(session_id),
            USAGE_PROVIDER_KEY: getattr(options, "aih_provider_id", None),
        }
        sdk_session_id = resume_id  # Start with existing SDK session ID if resuming
        # Persist-on-init guard (see stream_to_websocket): write the SDK session id
        # to the sessions row the moment it's captured so a mid-turn crash can't
        # orphan the session.
        sdk_link_persisted = False
        interrupted = False
        # Set when the turn dies mid-stream; the `error` frame is then the
        # terminal frame and no `done` follows (same as the WS path).
        crashed = False
    except BaseException:
        state.is_streaming = False
        state.streaming_started_at = None
        raise

    try:
        prepare_completions = getattr(
            state.adapter, "prepare_background_completions", None
        )
        if callable(prepare_completions):
            prepared = prepare_completions(
                getattr(options, "aih_background_task_completions", None)
            )
            if asyncio.iscoroutine(prepared):
                await prepared
        set_turn_source = getattr(state.adapter, "set_turn_source", None)
        if callable(set_turn_source):
            set_turn_source(getattr(options, "aih_turn_source", None))
        await state.adapter.send_query(prompt)

        async for message in state.adapter.receive_events():
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
                    if session_id and not sdk_link_persisted:
                        try:
                            # launched_model persists on EVERY spawn/init,
                            # including fresh-subprocess resumes (see execute_query
                            # for the host-op-guard rationale). sdk_session_id
                            # linking stays resume-gated.
                            update_kwargs = {
                                "launched_model": getattr(options, "model", None),
                            }
                            if resume_id is None:
                                update_kwargs["sdk_session_id"] = sdk_session_id
                            await run_sync(
                                database.update_session,
                                session_id,
                                **update_kwargs,
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
                        yield {"type": "text", "content": block.text}

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

                metadata["model"] = message.model

                # Per-turn usage from AssistantMessage.
                # Subagent (sidechain) messages carry parent_tool_use_id and run
                # their own, separate context window — counting them here would
                # overwrite the parent session's last-call figures with an
                # unrelated number, so they are skipped entirely.
                if message.usage and not getattr(message, "parent_tool_use_id", None):
                    # Track per-API-call values (overwrite → last call wins).
                    # Used by _persist_turn_results as the real context-window
                    # size; ResultMessage.usage is cumulative and unusable here.
                    # A snapshot-only record describes a call that was already
                    # billed, so its zeroes must not blank the last-call size —
                    # but it still ships, to carry the breakdown it exists for.
                    if not _is_context_snapshot_only(message.usage):
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

            elif isinstance(message, RateLimitEvent):
                logger.debug(f"Rate limit event: status={message.rate_limit_info.status}, type={message.rate_limit_info.rate_limit_type}")
                _rli = message.rate_limit_info
                # Yield the frame to clients on THIS install's WS. Cross-install
                # views (peers, offloaded/cloud-relayed sessions) do NOT receive
                # this frame — the event log never carries rate_limit. They see
                # the change only via the shared usage_snapshots row + cluster
                # broadcast we persist below.
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
                # Terminal reason + typed per-model usage pass-through
                metadata["terminal_reason"] = getattr(message, "terminal_reason", None)
                metadata["model_usage"] = getattr(message, "model_usage", None)
                # Surface provider-reported errors
                result_errors = getattr(message, 'errors', None)
                if result_errors:
                    metadata["errors"] = result_errors
                    logger.warning(f"ResultMessage contains errors: {result_errors}")
                # Surface HTTP error status code; emit before result frame
                api_err = getattr(message, "api_error_status", None)
                if api_err is not None:
                    metadata["api_error_status"] = api_err
                    yield {"type": "api_error", "status": api_err, "session_id": session_id}
                    # Funnel through the unified notification system (persist + the
                    # bell + all the user's devices). The frontend `api_error`
                    # handler does not emit separately — the `notification` event does.
                    await _emit_api_error_notification(
                        api_user_id, session_id, api_err,
                        delegated=_adapter_is_delegated(state.adapter),
                    )
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)
                    # Any call this turn with harness-estimated token counts
                    # (the adapter stamps the terminal usage); booked as a
                    # flag on the ledger row, never subtracted.
                    metadata["usage_estimated"] = bool(message.usage.get("usage_estimated"))

    except asyncio.CancelledError:
        interrupted = True
        logger.debug(f"Query interrupted for session {session_id}")
        yield {"type": "interrupted", "message": "Query was interrupted", "metadata": {"terminal_reason": metadata.get("terminal_reason")}}

    except (Exception, BaseExceptionGroup) as e:
        # Unwrap ExceptionGroup from an anyio.TaskGroup (Python 3.11+)
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
        crashed = True
        yield {"type": "error", "message": str(actual)}

    finally:
        # Mark as not streaming — keep in pool for reuse
        state.is_streaming = False
        state.streaming_started_at = None
        state.last_activity = datetime.now(timezone.utc)

        # Remove dead clients from pool
        if not state.is_connected:
            await pool.remove_dead(session_id)

    # Persist session data to database in a batched write (single transaction
    # for all tool messages + assistant response, instead of N+1 individual writes).
    # Detect an adapter-level interrupt: adapter.interrupt() causes receive_events()
    # to end naturally (no CancelledError), so `interrupted` is still False here.
    # Check the flag to correctly mark this turn as interrupted for persistence
    # and to yield the right terminal event to the frontend.
    sdk_interrupted = not interrupted and state.interrupt_requested
    if sdk_interrupted:
        interrupted = True

    await _persist_turn_results(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        metadata=metadata,
        profile_id=profile_id,
        interrupted=interrupted,
    )

    # Terminal history row for a turn that ran outside the scheduler — written
    # after the fact, never live (see turns.record_adhoc_turn). The early
    # returns above (busy-write guard, admission refusal, connect failure)
    # record nothing: they spent no provider tokens.
    try:
        _is_error = bool(metadata.get("is_error"))
        _aborted = interrupted or _is_error or not state.is_connected
        await run_sync(
            turns_db.record_adhoc_turn,
            session_id,
            "api_stream",
            outcome="aborted" if _aborted else "completed",
            prompt=prompt,
            profile_id=profile_id,
            project_id=project_id,
            stop_reason="interrupted" if interrupted else metadata.get("stop_reason"),
            error=str(metadata["errors"]) if (_is_error and metadata.get("errors")) else None,
            started_at=turn_started_at,
            # Context trail for scheduler-enqueued successors — see
            # turn_scheduler._enqueue_context_wake.
            overrides=overrides or None,
            payload={"api_user_id": api_user_id} if api_user_id else None,
        )
    except Exception as e:
        logger.warning(f"Failed to record ad-hoc turn for {session_id}: {e}")

    # Capture the context-usage breakdown so the context-usage endpoint can
    # serve a persisted snapshot after the subprocess is evicted from the pool.
    # Runs DETACHED (self-guards on the pool's live adapter) so it never delays
    # the terminal event below.
    schedule_context_usage_capture(session_id, log_prefix="")

    # Push agent-memory the CLI wrote this turn up to the shared DB.
    await push_memory(
        project_id, getattr(options, "cwd", None), memory_leg_for_options(options)
    )

    # Yield terminal event. CancelledError path already yielded 'interrupted'
    # at the top of the except block, and a crash already yielded 'error'; only
    # yield here for an adapter interrupt (no CancelledError) or normal completion.
    if sdk_interrupted:
        yield {"type": "interrupted", "message": "Query was interrupted"}
    elif not interrupted and not crashed:
        yield {
            "type": "done",
            "session_id": session_id,
            "metadata": metadata
        }


def pooled_client_lacks_approver(state: Any, broadcast_func: Any) -> bool:
    """True when an interactive turn would reuse a client a headless path built.

    Its adapter has no permission callback, and one cannot be added to a live
    client, so the turn needs a fresh one or its prompts go unanswered.
    """
    return bool(broadcast_func) and not getattr(state.adapter, "has_permission_callback", True)


async def _stream_to_websocket_impl(
    prompt: str,
    session_id: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    broadcast_func: Optional[callable] = None,
    api_user_id: Optional[str] = None,
    user_id: Optional[str] = None,
    upload_blob_ids: Optional[List[str]] = None,
    resume: bool = False,
    turn_source: Optional[str] = None,
    loop_guard_turn: Optional[Dict[str, Any]] = None,
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
        upload_blob_ids: Durable ``upload_blobs`` ids carried by the turn.
            The uploads were staged on ANOTHER install's disk; they are
            materialized into this session's working dir (once the cwd is
            resolved, below) and the prompt's uploader-absolute paths are
            rewritten to the local ones before anything reaches the engine.
        resume: Re-run the engine loop from the session head instead of sending
            a prompt. ``prompt`` is empty and nothing derived from it applies,
            so every step that reads or rewrites it is skipped.
        turn_source: The turn row's ``source`` (``user``, ``automation``,
            ``wakeup``...). Unattended sources count against the subscription
            loop guard (``app.core.loop_guard``).
        loop_guard_turn: The turn row itself, kept by the loop guard when it
            holds the turn for confirmation.
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

    from app.core.session_modes import turn_permission_mode

    # Keep provenance separately from the resolved mode passed to the builder.
    # No source or parent heuristic can distinguish copied user messages.
    permission_context = dict(overrides or {})
    config = profile.get("config", {})
    permission_mode = turn_permission_mode(permission_context, session, config, resume=resume)
    overrides = {**(overrides or {}), "permission_mode": permission_mode}

    # A chat session is pinned to 'default' whatever that chain produced — its
    # composer has no tool-access control to undo an inherited 'plan' (which the
    # chat ceiling would deadlock) or an inherited bypass (which would switch off
    # the very callback the ceiling leans on). Same coercion as
    # build_options_from_profile, applied here because this build is independent.
    from app.core.session_modes import (
        narrow_tool_access_for_mode,
        permission_mode_for_session_mode,
        session_mode_of,
    )

    permission_mode = permission_mode_for_session_mode(
        permission_mode, (session or {}).get("mode")
    )

    logger.debug(f"[WS] Permission mode for session {session_id}: {permission_mode} (session binding: {(session or {}).get('permission_mode')}, agent config: {config.get('permission_mode')}, override: {overrides.get('permission_mode') if overrides else None})")
    logger.debug(f"[WS] broadcast_func provided: {broadcast_func is not None}")

    # Get tool restrictions for use in callback. The mode decides,
    # not list emptiness — "Disable all tools" resolves to disallowed=["*"], so
    # the permission callback below denies everything instead of reading an
    # empty allow list as "unrestricted".
    from app.core.tool_access import resolve_tool_access

    _tool_access = resolve_tool_access(config)
    # ...and the session's sidepanel mode narrows that, exactly as
    # ``build_options_from_profile`` does. This build is independent of the one
    # inside the options builder, so without the same narrowing the callback
    # would happily approve a tool the options surface excluded — a real
    # divergence, not a redundancy.
    session_mode = session_mode_of(session)
    _tool_access = narrow_tool_access_for_mode(_tool_access, session_mode)
    from app.core.session_modes import narrow_tool_access_for_proactive
    assistant_proactive = (overrides or {}).get("_assistant_proactive")
    _tool_access = narrow_tool_access_for_proactive(_tool_access, assistant_proactive)
    allowed_tools = list(_tool_access.allowed or [])
    disallowed_tools = list(_tool_access.disallowed or [])

    # --- Session Pool: Acquire or create the pooled adapter ---
    pool = get_session_pool()
    reusing_client = False
    state = None

    # Create a queue for plugin stderr messages to be broadcast to frontend
    # Plugin hooks run in the adapter's async loop but need to send messages to our streaming loop
    stderr_queue: asyncio.Queue = asyncio.Queue()

    # Build options once (without callback — callback is set later if needed).
    # can_use_tool/hooks default to None inside build_options_from_profile, so
    # the shared seam yields byte-for-byte identical options; hooks are attached
    # separately below (after can_use_tool is set, with an stderr callback).
    from app.core.sdk_options import build_session_options
    from app.core.query_options import ensure_pending_checkout
    # Async wait for an in-flight ensure_worktree. Must stay on the event
    # loop: a sync poll would occupy a run_sync worker for 30s.
    if session:
        await ensure_pending_checkout(session)
    # Checkout setup can suspend while the owner changes the binding or
    # revokes an explicit choice on this live turn.
    session = await run_sync(database.get_session, session_id)
    from app.core.session_runtime import get_live_runtime
    runtime = get_live_runtime(session_id)
    if loop_guard_turn and runtime and runtime.turn_id == loop_guard_turn.get("id"):
        permission_context = runtime.turn.get("overrides") or {}
    permission_mode = turn_permission_mode(permission_context, session, config, resume=resume)
    overrides = {**(overrides or {}), "permission_mode": permission_mode}
    _build_opts_start = time.monotonic()
    try:
        # Off the event loop — see execute_query: the build can run git
        # fetch/worktree add and several DB reads synchronously.
        options, agents_dict = await asyncio.to_thread(
            build_session_options,
            profile=profile,
            project=project,
            overrides=overrides,
            resume_session_id=resume_id,
            api_user_id=api_user_id,
            session_id=session_id,
        )
    except (WorktreeUnavailableError, ValueError) as e:
        # A worktree session whose worktree can't be recovered, or a working
        # directory that doesn't exist (e.g. a project path from another
        # install): refuse to run in the main checkout / with an invalid cwd.
        # Surface a clear error to the user instead.
        logger.error(f"[WS] Cannot build session options for {session_id}: {e}")
        yield {
            "type": "error", "message": str(e),
            **({"code": "checkout_unavailable"} if isinstance(e, WorktreeUnavailableError) else {}),
        }
        return
    _build_opts_elapsed = time.monotonic() - _build_opts_start
    if _build_opts_elapsed > 0.5:
        logger.warning(f"[WS] build_options_from_profile took {_build_opts_elapsed:.2f}s for session {session_id}")

    # Credential gate: every turn starts here whatever its source (chat,
    # connector, automation, wake-up, worker, a turn handed over from another
    # device). No license or no credential for the active Claude Code mode
    # refuses the turn with a setup message; nothing falls back.
    from app.core import credential_gate, loop_guard

    _leg = await asyncio.to_thread(credential_gate.leg_for_options, options)
    _refusal = await credential_gate.turn_refusal_async(_leg)
    if _refusal:
        logger.warning(f"[WS] Turn refused for session {session_id}: {_refusal}")
        yield credential_gate.refusal_event(_refusal)
        return

    # Subscription loop guard: an unattended turn (automation, wake-up,
    # connector, or a worker one of those started) on the Claude CLI in
    # subscription mode past its hourly budget waits for the owner's
    # confirmation instead of starting.
    if turn_source in loop_guard.UNATTENDED_SOURCES:
        _guard_payload = (loop_guard_turn or {}).get("payload") or {}
        _confirmed = bool(_guard_payload.get(loop_guard.CONFIRMED_FLAG))
        _held = await asyncio.to_thread(
            loop_guard.check, session_id, turn_source, _leg, confirmed=_confirmed,
            origin=_guard_payload.get(loop_guard.ORIGIN_KEY),
        )
        if _held:
            if loop_guard_turn:
                await asyncio.to_thread(loop_guard.hold, loop_guard_turn, _held)
            yield {"type": "error", "message": _held, "code": loop_guard.HOLD_CODE}
            return

    # The owner's assistant limits: past the daily spending limit the
    # assistant home starts no new turn, nor does an unattended turn in a job
    # it started (or one past its job time limit).
    from app.core import assistant_limits

    _limited = await asyncio.to_thread(assistant_limits.turn_refusal, session_id, turn_source)
    if _limited:
        logger.warning(f"[WS] Turn refused for session {session_id}: assistant limit")
        yield {"type": "error", "message": _limited, "code": "assistant_limit"}
        return

    # Cross-install uploads: the turn's blobs were lifted on the SENDING
    # install; write them into the now-resolved working dir and rewrite the
    # prompt's uploader-absolute paths to the local ones. Done here — after the
    # cwd authority (build_session_options) has spoken and before the prompt
    # reaches any engine — so there is exactly one resolved cwd in play. A
    # failure REFUSES the turn the same way an unavailable worktree does:
    # running with dead attachment paths would silently lose the attachments.
    if upload_blob_ids and not resume:
        from app.core import upload_transfer

        try:
            _mapping = await asyncio.to_thread(
                upload_transfer.materialize_blobs_for_turn,
                list(upload_blob_ids),
                str(getattr(options, "cwd", "") or ""),
            )
            prompt = upload_transfer.rewrite_prompt(prompt, _mapping)
        except Exception as e:  # noqa: BLE001 — surface, never run with dead paths
            logger.error(
                f"[WS] Upload materialization failed for {session_id}: {e}"
            )
            yield {
                "type": "error",
                "message": f"Couldn't prepare this message's attachments: {e}",
            }
            return

    # Resolve the pooled state after setup; eviction may run during its awaits.
    existing_state = pool.get(session_id)
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
        # Candidate for reuse — unless a headless path built it without an
        # approver (``pooled_client_lacks_approver``) or critical options changed
        missing_approver = pooled_client_lacks_approver(existing_state, broadcast_func)
        if not missing_approver and not requires_reconnect(existing_state, options):
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
            # entered, ANY exit (exception or cancellation — the set_model await
            # below is a suspension point) must clear the flag.
            # A stranded is_streaming=True bricks the pooled client forever:
            # every future query gets the busy error, evict_idle skips
            # streaming states, and report_long_streaming only logs.
            try:
                # Update callback refs so the existing can_use_tool closure sees current values
                state.callback_refs.update({
                    "broadcast_func": broadcast_func,
                    "permission_mode": permission_mode,
                    "allowed_tools": allowed_tools,
                    "disallowed_tools": disallowed_tools,
                    # Re-stamped like the tool lists: the pooled client outlives
                    # a single turn, so a stale mode here would let the callback
                    # enforce the wrong lane's ceiling on a reused subprocess.
                    "session_mode": session_mode,
                    "profile_id": profile_id,
                    "config": config,
                    "session_id": session_id,
                    "user_id": user_id,
                    # Reset the outstanding-prompt counter at the start of each turn so a
                    # prior turn that was hard-cancelled mid-prompt (its finally clear
                    # interrupted) can't leave a leaked count that pins the indicator on.
                    "attention_count": 0,
                })

                # Handle runtime-changeable options via adapter methods
                # The adapter writes each applied model / permission-mode change
                # back to these options (HarnessAdapter._record_applied), so they
                # hold the live values, not the connect-time ones.
                old_opts = state.connect_options
                if old_opts:
                    new_model = getattr(options, 'model', None)
                    old_model = getattr(old_opts, 'model', None)
                    # Belt-and-braces provider gate: requires_reconnect()
                    # already forces a respawn when the resolved binding changes,
                    # so this branch is unreachable on a provider switch — but a
                    # set_model() on a client built for a DIFFERENT provider would
                    # silently send an unknown model id to the wrong upstream, so
                    # the hot-swap is spelled out as same-provider-only.
                    _same_provider = getattr(old_opts, 'aih_provider_id', None) == getattr(options, 'aih_provider_id', None)
                    if new_model and new_model != old_model and _same_provider:
                        try:
                            # Live hot-swap stays within the session's current provider;
                            # a provider change is caught by requires_reconnect() and
                            # forces a respawn instead of reaching this set_model() call.
                            await state.adapter.set_model(new_model)
                            logger.debug(f"[WS] Updated model to {new_model} for reused session {session_id}")
                            # Persist the switched model so launched_model never
                            # goes stale on a mid-life model switch — the host-op
                            # guard reads this column directly.
                            try:
                                await run_sync(database.update_session, session_id, launched_model=new_model)
                            except Exception:
                                pass
                        except Exception as e:
                            logger.warning(f"[WS] Failed to set model on reused session {session_id}: {e}")

                    # Reconcile permissions only after all setup awaits, in the
                    # protected query block below. Never replay an early snapshot
                    # over a successful owner change, or query after setter failure.

                logger.info(f"[WS] Reusing pooled client for session {session_id}")
            except BaseException:
                state.is_streaming = False
                state.streaming_started_at = None
                raise
        else:
            # Options changed — need fresh subprocess
            reasons = reconnect_option_names(existing_state, options)
            if missing_approver:
                reasons.append("permission_callback")
            logger.info("[WS] Options changed for session %s (%s), creating fresh client",
                        session_id, ",".join(reasons))
            await pool.remove(session_id, preserve_session=True)
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
            # The session's sidepanel lane. Lives here, beside the tool lists,
            # because the chat ceiling has a path half the lists cannot express
            # and the reuse path re-stamps it every turn.
            "session_mode": session_mode,
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
                # Read the mode the way the session's leg runs it: a legacy
                # ``auto`` is Ask everywhere but the delegated Claude CLI, and
                # the engine already asks under Ask — approving here would
                # turn every such prompt into an allow. A caller that does not
                # say which leg it is on gets the mode unchanged.
                from app.core.session_modes import (
                    effective_permission_mode,
                    mode_approves_without_prompt,
                )

                _perm_mode = effective_permission_mode(
                    refs["permission_mode"],
                    delegated=getattr(context, "delegated", True),
                )
                _broadcast = refs["broadcast_func"]
                _profile_id = refs["profile_id"]
                _config = refs["config"]
                _sid = refs["session_id"]
                _user_id = refs.get("user_id")

                # Extract tool_use_id and agent_id from context
                # These enable disambiguating parallel permission requests
                _ctx_tool_use_id = getattr(context, 'tool_use_id', None)
                _ctx_agent_id = getattr(context, 'agent_id', None)

                # Extract ToolPermissionContext enrichment fields
                _ctx_decision_reason = getattr(context, 'decision_reason', None)
                _ctx_blocked_path    = getattr(context, 'blocked_path', None)
                _ctx_title           = getattr(context, 'title', None)
                _ctx_display_name    = getattr(context, 'display_name', None)
                _ctx_description     = getattr(context, 'description', None)

                # Check profile-level tool restrictions FIRST. Entries may be
                # exact names or fnmatch patterns (``mcp__browser__*``, ``*``),
                # so matching goes through the shared helper rather than a
                # bare ``in``.
                from app.core.tool_access import matches_any

                if _disallowed and matches_any(tool_name, _disallowed):
                    logger.debug(f"[WS] Tool {tool_name} blocked by profile disallowed_tools")
                    return PermissionResultDeny(message=f"Tool '{tool_name}' is disabled in this profile")
                if _allowed and not matches_any(tool_name, _allowed):
                    logger.debug(f"[WS] Tool {tool_name} blocked by profile allowed_tools (not in whitelist)")
                    return PermissionResultDeny(message=f"Tool '{tool_name}' is not enabled in this profile")

                # The path half of the chat ceiling. The name half above put
                # Read on a chat session's surface so the model can open the
                # files the user attached; that is the ONLY thing it may open.
                # Reading the lane out of refs (not out of the enclosing scope)
                # is what keeps a pooled client from enforcing the lane it was
                # first built for after being reused by another session.
                #
                # This is the THIRD line of defence, not the first: neither
                # runtime consults this callback for a call it has already
                # decided to allow, so the fence is enforced primarily by the
                # fluid rule pair (native) and by the cwd plus the withheld bare
                # grant (delegated) — see ``app.core.session_modes``. Both of
                # those funnel the calls they cannot decide to HERE, which is
                # why the same predicate has to answer in all three places.
                if refs.get("session_mode") == "chat":
                    from app.core.session_modes import (
                        CHAT_MODE_READ_TOOLS,
                        chat_mode_read_permitted,
                    )

                    if not chat_mode_read_permitted(tool_name, tool_input):
                        logger.debug(
                            f"[WS] Tool {tool_name} blocked: chat session read "
                            f"outside the chat uploads directory"
                        )
                        return PermissionResultDeny(
                            message=(
                                "A chat session can only read files attached to "
                                "the chat itself. Its whole tool surface is web "
                                "search, web fetch, and reading those "
                                "attachments — open this path from a code "
                                "session instead."
                            )
                        )
                    if tool_name in CHAT_MODE_READ_TOOLS:
                        # Inside the fence: approve without prompting. The user
                        # uploaded this file to this chat a moment ago, and the
                        # runtimes only route a read here when their own glob
                        # was not certain enough to allow it themselves — asking
                        # would be a prompt about the one thing the lane exists
                        # to do, with no other answer available.
                        return PermissionResultAllow(updated_input=tool_input)

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
                    until the LAST prompt resolves. The counter update itself needs
                    no lock (no await before it, single-threaded loop), but the
                    durable read below does await, so the read and the broadcast
                    run under a FIFO lock and re-read the counter: the last bump
                    broadcasts last, with the newest in-memory and durable state,
                    instead of a stale non-zero count landing after a zero."""
                    if not _user_id:
                        return
                    callback_refs["attention_count"] = max(
                        0, (callback_refs.get("attention_count") or 0) + delta
                    )
                    lock = callback_refs.get("attention_lock")
                    if lock is None:
                        lock = callback_refs["attention_lock"] = asyncio.Lock()
                    async with lock:
                        # Posted questions outlive this turn's counter, so never
                        # report fewer than the durable pending rows: without this,
                        # resolving a permission broadcast 0 and erased "Has
                        # question" while its row was still pending.
                        try:
                            from app.core.user_question_handler import get_session_attention

                            durable = await asyncio.to_thread(get_session_attention, _sid)
                        except Exception:
                            durable = None
                            logger.debug("[WS] durable attention read failed", exc_info=True)
                        n = callback_refs.get("attention_count") or 0
                        try:
                            count, out_kind = n, kind
                            if durable is not None:
                                _owner, d_count, d_kind = durable
                                if d_count > count:
                                    count = d_count
                                if delta < 0 or d_kind == "permission":
                                    # The prompt just resolved (or a permission is
                                    # open): label by what is still pending.
                                    out_kind = d_kind if d_count else kind
                            await sync_engine.broadcast_session_attention(
                                _user_id, _sid, count, kind=out_kind
                            )
                        except Exception:
                            logger.debug("[WS] session_attention broadcast failed", exc_info=True)

                async def _request_permission(**kwargs):
                    """Show a permission prompt, wrapped with the awaiting-input
                    indicator so a backgrounded session blocked on approval is just
                    as discoverable as one blocked on a question. Clears the
                    indicator whether the prompt is answered, deferred, or the query
                    is cancelled (session closed).

                    ``route_permission`` is a pass-through for every
                    ordinary session — it calls the SAME
                    ``permission_handler.request_permission`` with the SAME
                    kwargs. It diverges only when THIS session was minted as a
                    ProviderAgent worker, in which case the prompt is raised on
                    (and answerable from) the PARENT session's UI, tagged with the
                    worker's label. See ``app/core/provider_agent_approvals``."""
                    from app.core import provider_agent_approvals

                    await _bump_attention(1, "permission")
                    try:
                        return await provider_agent_approvals.route_permission(
                            kwargs, permission_handler.request_permission
                        )
                    finally:
                        await _bump_attention(-1, "permission")

                # Handle AskUserQuestion (SDK) and AskUser (fluid)
                # specially — post durable questions without holding the turn.
                from app.core.user_question_handler import (
                    FLUID_ANSWER_KEY,
                    FLUID_QUESTION_TOOLS,
                    QUESTION_TOOLS,
                    fluid_answer_from_envelope,
                    questions_from_tool_input,
                )

                if tool_name in QUESTION_TOOLS:
                    questions = questions_from_tool_input(tool_input)
                    if not questions:
                        return PermissionResultAllow(updated_input=tool_input)

                    request_id = (
                        getattr(context, "question_request_id", None)
                        or f"question-{_sid}-{uuid.uuid4().hex[:8]}"
                    )
                    logger.debug(f"[WS] {tool_name} canUseTool triggered: {request_id} with {len(questions)} questions (tool_use_id={_ctx_tool_use_id}, agent_id={_ctx_agent_id})")

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

                    if tool_name == "AskUserQuestion" or getattr(context, "nonblocking_question", False):
                        posted = await user_question_handler.post_question(
                            request_id=request_id,
                            tool_use_id=_ctx_tool_use_id or request_id,
                            session_id=_sid,
                            questions=questions,
                            broadcast_func=_broadcast,
                            fanout_func=_fanout_question,
                        )
                        if tool_name == "AskUserQuestion":
                            # The SDK has no posted tool-result variant. Denial
                            # ends its callback without executing a blocking UI.
                            return PermissionResultDeny(message=(
                                f"Question posted (request_id={request_id}). Continue working; "
                                "the answer will arrive as a user message. Do not ask again."
                            ))
                        return PermissionResultAllow(updated_input={**tool_input, **posted})

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

                    # The user closed the question modal ("X") instead of
                    # answering. There is no "no answer" value the
                    # AskUserQuestion tool accepts, so resolve the pending tool
                    # call by denying it — the SDK turns the deny message into
                    # the tool_result, which is exactly the outcome we want the
                    # model to read. Fluid AskUser have no deny
                    # path: they wait on RespondQuestion, so a dismiss is an
                    # empty answer (AskUser records "(no answer)").
                    # (Per-question SKIPS are not this: they come back as
                    # ordinary answer strings and flow through below.)
                    from app.core.user_question_handler import (
                        DISMISSED_KEY,
                        DISMISS_MESSAGE,
                    )

                    if isinstance(answers, dict) and answers.get(DISMISSED_KEY):
                        logger.debug(f"[WS] Question {request_id} dismissed by user")
                        if tool_name in FLUID_QUESTION_TOOLS:
                            return PermissionResultAllow(
                                updated_input={**tool_input, FLUID_ANSWER_KEY: ""}
                            )
                        return PermissionResultDeny(message=DISMISS_MESSAGE)

                    logger.debug(f"[WS] Got answers for {request_id}: {list(answers.keys())}")
                    updated = {**tool_input, "answers": answers}
                    if tool_name in FLUID_QUESTION_TOOLS:
                        updated[FLUID_ANSWER_KEY] = fluid_answer_from_envelope(answers)
                    return PermissionResultAllow(updated_input=updated)

                # A high-risk call in an owner's-assistant session: the adapter
                # set this flag from app.core.assistant_permissions, never the
                # model. It goes to the owner whatever the mode, past saved
                # rules; routing puts it in the assistant's conversation, where
                # only a firm yes approves it (app.core.assistant_confirm).
                if getattr(context, "assistant_owner", False):
                    result = await _request_permission(
                        request_id=f"perm-{_sid}-{uuid.uuid4().hex[:8]}",
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
                        skip_saved_rules=True,
                    )
                    if isinstance(result, _DeferSentinel):
                        return PermissionResultDeny(message="Not approved: the owner was not available to answer.")
                    return result

                # Computer use: request_access is the single approval gate (keyed to
                # permission mode); individual actions are auto-allowed here — they
                # enforce the granted state themselves, so we never prompt per click.
                if tool_name.startswith("mcp__computer-use__"):
                    if tool_name == "mcp__computer-use__request_access":
                        # An allow here is recorded for the tool when fluid's
                        # gate lets the call through (HarnessAdapter.
                        # _on_tool_allowed), not here, so it counts once.
                        # The session's FIRST grant is different: it always
                        # goes to a person, in every mode and past any saved
                        # rule, and the person's allow is recorded here for
                        # the tool to spend (see computer_use.access). A worker
                        # whose desktop only agents use opts out: nobody is at
                        # that screen to protect or to answer.
                        from app.api.computer_use import is_dedicated_desktop
                        from app.mcp.computer_use import access as cu_access

                        first_use = (
                            not cu_access.is_granted(_sid)
                            and not await run_sync(is_dedicated_desktop)
                        )
                        if _perm_mode == "bypassPermissions" and not first_use:
                            return PermissionResultAllow()
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
                            skip_saved_rules=first_use,
                        )
                        if isinstance(result, _DeferSentinel):
                            return PermissionResultDeny(message="Computer-use access deferred by user")
                        if getattr(result, "behavior", None) == "deny":
                            return result
                        if first_use:
                            cu_access.confirm_first_use(_sid)
                        return PermissionResultAllow()
                    # All other computer-use actions: access is gated by request_access.
                    return PermissionResultAllow()

                # Every other tool: Ask routes it through the permission
                # handler; the modes that approve on their own allow it (the
                # same rule a native run with no approver applies) — unless a
                # remembered Deny rule names it. Saved deny rules beat every
                # mode short of Bypass, as Claude Code's deny rules beat its
                # modes; Ask already applies them inside request_permission.
                if not mode_approves_without_prompt(_perm_mode, tool_name):
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
                        mode_managed=True,
                    )
                    if isinstance(result, _DeferSentinel):
                        # Return PreToolUseHookSpecificOutput with defer
                        try:
                            from app.core.agent_types import PreToolUseHookSpecificOutput
                            return PreToolUseHookSpecificOutput(
                                hookEventName="PreToolUse",
                                permissionDecision="defer",
                                permissionDecisionReason="Deferred by user",
                            )
                        except (ImportError, TypeError):
                            # Fallback: if the signature differs, treat as deny so we never crash
                            return PermissionResultDeny(message="Tool deferred by user")
                    return result
                else:
                    if _perm_mode != "bypassPermissions":
                        rule = await asyncio.to_thread(
                            permission_handler.saved_rule, _sid, _profile_id, tool_name, tool_input,
                        )
                        if rule is not None and rule.decision == PermissionDecision.DENY:
                            logger.debug(f"[WS] {tool_name} denied by saved rule under {_perm_mode}")
                            return PermissionResultDeny(message="Denied by saved rule")
                    return PermissionResultAllow()

        # Set the callback on the already-built options (avoid redundant rebuild)
        options.can_use_tool = can_use_tool_callback

        # Build hooks configuration from all sources (plugins, database)
        from app.core.hook_service import attach_hooks_to_options

        async def stderr_callback(plugin_name: str, stderr_output: str, severity: str):
            """Callback for plugin hooks to broadcast stderr to frontend."""
            await stderr_queue.put({
                "type": "plugin_message",
                "plugin": plugin_name,
                "message": stderr_output,
                "severity": severity
            })

        attach_hooks_to_options(
            options, project_id=project_id, stderr_callback=stderr_callback
        )

        # Admission backpressure: refuse a NEW session if the box is at its
        # process/RAM/pool ceiling, rather than killing in-flight work later.
        # psutil process-tree walk — off the loop.
        # Lane-aware: the pool's subprocess cap counts DELEGATED sessions
        # only, and deciding the leg can read the provider row, so both
        # run in the same worker thread.
        incoming_lane = await asyncio.to_thread(memory_leg_for_options, options)
        refusal = await asyncio.to_thread(pool.admission_check, incoming_lane)
        if refusal is not None:
            logger.warning(f"[Admission] Refusing new session {session_id}: {refusal}")
            yield {"type": "error", "message": f"Server is at capacity — {refusal}. Please retry shortly."}
            return

        # Evict LRU session if pool is full
        evicted = await pool.evict_if_needed(incoming_lane)
        if evicted:
            logger.debug(f"[WS] Evicted LRU session {evicted} to make room for {session_id}")

        # Create and connect new client
        logger.debug(f"[WS] Creating fluid harness adapter for session {session_id} (resume={resume_id is not None}, include_partial={options.include_partial_messages})")
        logger.debug(f"[WS] Options cwd: {options.cwd}")

        # Verify CWD exists before spawning subprocess
        if options.cwd and not Path(options.cwd).is_dir():
            logger.error(f"[WS] CWD does not exist: {options.cwd}")
            yield {"type": "error", "message": f"Working directory does not exist: {options.cwd}"}
            return

        # Attribute this session to tasks created inside adapter.connect() (see
        # the chat path above / error_log_handler.emit).
        set_current_session_id(session_id)

        # Wrap stderr callback to collect lines for error reporting
        options.stderr, cli_stderr_lines = _wrap_stderr_capture(options.stderr)

        # Pull shared agent-memory from the DB into the local memory dir before
        # the CLI reads it, so this install sees memories written elsewhere.
        await reconcile_memory(project_id, options.cwd, memory_leg_for_options(options))

        # Backend selection via the provider seam: make_adapter() always
        # returns the fluid HarnessAdapter (see app/core/provider/factory.py).
        from app.core.provider.factory import make_adapter
        adapter = make_adapter()
        logger.debug("[WS] provider adapter created (%s), attempting connect...", type(adapter).__name__)

        try:
            _connect_start = time.monotonic()
            await asyncio.wait_for(adapter.connect_with_sdk_options(options), timeout=60.0)
            _connect_elapsed = time.monotonic() - _connect_start
            logger.info(f"[WS] SDK connect for session {session_id}: {_connect_elapsed:.1f}s (reuse=False)")
        except ClaudeCliMissingError as e:
            # Setup problem, not a crash: no traceback, the message is the fix.
            logger.warning(f"[WS] Claude CLI missing for session {session_id}: {e}")
            yield {"type": "error", "message": str(e)}
            return
        except Exception as e:
            import traceback
            logger.error(f"[WS] Failed to connect for session {session_id}: {e}")
            logger.error(f"[WS] Exception type: {type(e).__name__}")
            logger.error(f"[WS] Full traceback:\n{traceback.format_exc()}")
            # Check if the CLI subprocess is still alive
            if adapter.is_alive():
                logger.error("[WS] CLI process is still running (hung during initialization)")
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
        # Note: CLI built-in commands like /context and /compact are passed directly to the engine
        task_tool_uses = {}  # Track Task (subagent) tool uses by tool_id
        # Turn-local set of Task/Agent tool_use ids whose spawn is APP-DISPATCHED
        # (fluid's bare-named "Task" builtin, routed through the host dispatcher).
        # For those, WorkerFeed emits the canonical subagent_start/subagent_done
        # keyed on the worker session id, so this loop must NOT emit its own
        # generic pair — see the `block.name in ("Task", "Agent")` branch below.
        app_dispatch_tool_ids: set = set()
        # Defer-don't-terminate gate. A launch ToolResult with no evidence
        # of being background OR foreground is PARKED here instead of firing a
        # (possibly premature) subagent_done: tool_use_id -> {output, is_error,
        # agent_type}. ``task_started_tool_use_ids`` records every tool_use_id
        # whose TaskStartedMessage arrived in-stream THIS turn. Reconciliation:
        # a parked entry whose TaskStarted arrives is DROPPED (it settles on its
        # terminal TaskNotificationMessage via the tool_use_id join); at a
        # NORMAL end-of-turn drain, a surviving parked entry that never saw
        # TaskStarted and isn't in any background set is a genuine synchronous
        # foreground launch → flushed as subagent_done. On interrupt/error the
        # flush is skipped and parked entries are dropped (never persist a
        # terminal on an aborted path).
        deferred_subagent_done: Dict[str, Dict[str, Any]] = {}
        task_started_tool_use_ids: set = set()
        # Seed cross-turn BACKGROUND sub-agents. Background sub-agents
        # (run_in_background=true) OUTLIVE the turn that launched them: in a
        # FOLLOW-UP turn their StreamEvents/AssistantMessages still arrive with
        # parent_tool_use_id set, but the launching Agent/Task tool_use block was
        # seen in a PRIOR turn and is absent from this fresh empty dict. Lane
        # routing itself does not depend on this seed — every subagent gate
        # routes on a non-null parent_tool_use_id alone (parent-null-based), so
        # foreign events reach the subagent lane whether or not their launch id is
        # seeded. The seed still has two consumers: (a) the
        # `tool_use_id in task_tool_uses` launch-completion/ACK branches
        # below — a seeded entry (run_in_background=True) makes a cross-turn
        # launch ACK skip subagent_done and keep the node RUNNING; and (b) the
        # SystemMessage `len(task_tool_uses) > 0` suppression heuristic, which treats
        # system messages as subagent context while any tracked task is still live.
        # Union the durable _background_launches set (cleared only with the session)
        # with the parent tool_use ids of all in-flight background work
        # (get_active_background_work — the same view the park gate uses), to
        # catch a launch whose tool_use_id both arming heuristics missed (the
        # run_in_background input flag AND the ACK text-sniff) but which is still a
        # RUNNING task. Seeded entries carry run_in_background=True and _cross_turn
        # (their launch was seen in a prior turn).
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
        metadata = {
            "usage_settlement_key": await capture_usage_identity(session_id),
            USAGE_PROVIDER_KEY: getattr(options, "aih_provider_id", None),
        }
        interrupted = False
        # Last `model` value emitted via a `model_info` system note THIS turn.
        # Seeded from the init frame so init + first AssistantMessage with
        # the same model produce ONE event (not two); a mid-turn model CHANGE
        # (rerouting) re-emits. Main-lane only (subagent models are their own).
        last_model_info: Optional[str] = None
        requested_model = getattr(options, "model", None)
        # Resolved provider for this turn (None = native Anthropic). Read off the
        # OPTIONS object, which carries the binding query_options actually
        # resolved (`aih_provider_id`) — session binding, an explicit provider
        # override, a `prov:` composite model pick that rebound it, or a native
        # Claude pick that CLEARED it. Re-deriving it from overrides/config here
        # would be wrong in every one of those cases: the frontend suppresses
        # the "rerouted" pill on a non-null provider and
        # scopes its context-window lookup by it, so a stale value both
        # false-flags and mis-sizes the bar.
        session_provider = getattr(options, "aih_provider_id", None)

        def _model_info_frame(model: str) -> dict:
            return {
                "type": "system",
                "subtype": "model_info",
                "data": {
                    "model": model,
                    "requested_model": requested_model,
                    "provider": session_provider,
                },
            }

        include_partial = options.include_partial_messages  # Track if streaming events are enabled
        has_visible_content = False  # Track if any content was yielded to the frontend
        is_slash_command = not resume and prompt.strip().startswith("/")

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
        # Resolve virtual @Notes/note-id references to actual content. A resumed
        # turn has no prompt to resolve — the head node already carries whatever
        # the failed call was sent.
        if not resume:
            prompt = _resolve_note_references_for(prompt, api_user_id)

        _query_send_start = time.monotonic()
        prepare_completions = getattr(
            state.adapter, "prepare_background_completions", None
        )
        if callable(prepare_completions):
            prepared = prepare_completions(
                getattr(options, "aih_background_task_completions", None)
            )
            if asyncio.iscoroutine(prepared):
                await prepared
        set_turn_source = getattr(state.adapter, "set_turn_source", None)
        if callable(set_turn_source):
            set_turn_source(getattr(options, "aih_turn_source", None))
        # Serialize with the owner's live setter, and reconcile after every
        # setup await (checkout, option build, model switch and connect). The
        # read and comparison have no intervening await; a failed setter goes
        # through the normal turn-error cleanup, never a looser query.
        from app.api.sessions_permissions import _locks as permission_locks
        from app.core.session_runtime import get_live_runtime

        async with permission_locks.setdefault(session_id, asyncio.Lock()):
            latest_session = await run_sync(database.get_session, session_id)
            runtime = get_live_runtime(session_id)
            if loop_guard_turn and runtime and runtime.turn_id == loop_guard_turn.get("id"):
                permission_context = runtime.turn.get("overrides") or {}
            permission_mode = turn_permission_mode(permission_context, latest_session, config, resume=resume)
            # The owner's assistant (its home, the chats it starts and their
            # workers) runs its engine in Bypass whatever the binding says, and
            # its own approver asks the owner for the high-risk calls
            # (``build_options_from_profile``). Keep the builder's decision for
            # the engine; the callback keeps the resolved mode, as before.
            engine_mode = ("bypassPermissions"
                           if isinstance(getattr(options, "aih_assistant_scope", None), dict)
                           else permission_mode)
            applied_mode = getattr(state.connect_options, "permission_mode", None)
            if engine_mode != applied_mode:
                await state.adapter.set_permission_mode(engine_mode)
            options.permission_mode = engine_mode
            if state.connect_options is not None:
                state.connect_options.permission_mode = engine_mode
            state.callback_refs["permission_mode"] = permission_mode
            if resume:
                await state.adapter.send_resume()
            else:
                await state.adapter.send_query(prompt)
        _query_send_elapsed = time.monotonic() - _query_send_start
        if _query_send_elapsed > 1.0:
            logger.warning(f"[WS] client.query() took {_query_send_elapsed:.1f}s for session {session_id}")

        _stream_start = time.monotonic()
        _event_count = 0
        async for message in state.adapter.receive_events():
            # Don't break immediately on interrupt_requested — let receive_events()
            # end naturally so remaining in-flight tool calls reach the UI.
            # The adapter's interrupt() causes receive_events() to end after
            # completing any in-progress operations.

            _event_count += 1
            if _event_count == 1:
                # The delegated CLI is running by its turn's first event (the
                # first turn spawns it). Make it, and the subagents it forks
                # (they inherit the score), the kernel's preferred OOM-kill
                # target, so a host OOM reaps one disposable session subprocess
                # instead of the orchestrator or the database. Best-effort; a
                # no-op for native sessions or when disabled.
                mark_subprocess_oom_killable(state)
                # Remember the CLI on disk, so a restart after an abrupt
                # server death stops it (and what it started) rather than
                # leaving orphans. Idempotent per process; a native session
                # has no subprocess and records nothing.
                _cli_pid = subprocess_pid(state)
                if _cli_pid:
                    from app.core import process_ledger
                    await asyncio.to_thread(
                        process_ledger.record_process, _cli_pid, "agent_cli",
                        session_id=session_id,
                    )

            # Drain stderr queue and yield any plugin messages
            for stderr_msg in await drain_stderr_queue():
                logger.debug(f"[WS] Yielding plugin message to websocket: {stderr_msg.get('type')}")
                yield stderr_msg

            # Check typed task messages before generic SystemMessage
            # (they are subclasses and would match isinstance SystemMessage)
            # Mirror background-task lifecycle into the registry (TaskList / /live).
            _record_task_message(session_id, message)

            # STREAM_DEBUG: trace frame ordering ONLY while a background task
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

            if isinstance(message, SteerDeliveredEvent):
                # A queued message reached the model mid-turn. The frame carries
                # the correlation ids straight through to the runtime, which
                # owns the two durable consequences (record the user message at
                # THIS position; retire the queue row). Nothing here touches the
                # turn's own accumulators: a steer is the user speaking, not
                # assistant output, so it is neither visible content nor a
                # turn_event of this turn.
                yield {
                    "type": "steer_delivered",
                    "session_id": session_id,
                    "steers": list(message.steers or []),
                }
                continue

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
                # A TaskStarted for a parked launch confirms the task is a
                # real registered task — DROP the deferred entry (it now settles
                # on its terminal TaskNotificationMessage via the tool_use_id
                # join). Record the id so the end-of-turn drain flush never
                # flushes a launch that DID start (only a synchronous foreground
                # launch — one that emitted NO in-stream TaskStarted — is flushed).
                _ts_tuid = getattr(message, "tool_use_id", None)
                if _ts_tuid:
                    task_started_tool_use_ids.add(_ts_tuid)
                    deferred_subagent_done.pop(_ts_tuid, None)
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
                    if not sdk_link_persisted:
                        try:
                            # launched_model persists on EVERY spawn/init,
                            # including fresh-subprocess resumes (see execute_query
                            # for the host-op-guard rationale). sdk_session_id
                            # linking stays resume-gated.
                            update_kwargs = {
                                "launched_model": getattr(options, "model", None),
                            }
                            if resume_id is None:
                                update_kwargs["sdk_session_id"] = sdk_session_id
                            await run_sync(
                                database.update_session,
                                session_id,
                                **update_kwargs,
                            )
                            sdk_link_persisted = True
                        except Exception as link_err:
                            logger.warning(
                                f"[WS] early sdk_session_id link failed for "
                                f"{session_id}: {link_err}"
                            )

                    # Live model_info note: surface the model the SDK actually
                    # LAUNCHED (vs. what we requested) so the frontend can flag
                    # rerouting during the turn. Seed last_model_info so an
                    # identical first AssistantMessage does not re-emit.
                    init_model = message.data.get("model")
                    if init_model:
                        last_model_info = init_model
                        yield _model_info_frame(init_model)

                else:
                    # Forward all system messages to frontend except noisy internal ones.
                    # A whitelist would silently drop errors/warnings with new
                    # subtypes (e.g., skill errors, command output).
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
                    # A compact_boundary is the parent conversation's: the
                    # adapter only emits the main thread's compaction, which
                    # may land while a subagent is still tracked here.
                    is_from_subagent = is_subagent_lane or (
                        len(task_tool_uses) > 0 and message.subtype != "compact_boundary"
                    )

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
                # parent_tool_use_id is set on subagent events.
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
                        # Thinking block streaming. ``subtype`` (fluid
                        # BlockStart.subtype) tells the provider's own summary
                        # ("reasoning_summary") from the reasoning text.
                        subtype = delta.get("subtype") or None
                        yield {
                            "type": "stream_delta",
                            "delta_type": "thinking",
                            "content": delta["thinking"],
                            "index": event.get("index", 0),
                            **({"subtype": subtype} if subtype else {}),
                        }
                    elif delta_type == "input_json_delta" and delta.get("partial_json"):
                        # Tool input streaming
                        yield {
                            "type": "stream_delta",
                            "delta_type": "tool_input",
                            "content": delta["partial_json"],
                            "index": event.get("index", 0),
                            # Present on native-leg deltas; the delegated CLI
                            # leaves it out and the recorder falls back to the
                            # name it saw on the open block.
                            "tool_name": delta.get("name"),
                        }

                elif event_type == "message_start":
                    # Start of a new streaming message
                    logger.debug("[WS] StreamEvent message_start")
                    yield {
                        "type": "stream_start",
                        "message": event.get("message", {})
                    }

                elif event_type == "provider_alive":
                    # fluid's ProviderAlive: the provider accepted the request
                    # and is still streaming. Not visible content; the
                    # recorder keeps it as worker liveness and the runtime
                    # keeps it off the WS leg.
                    yield {
                        "type": "provider_alive",
                        "signal": event.get("signal"),
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

                        # Gate for the spurious-unpark fix: record the launch
                        # tool_use_id IFF the parent launched this tool with
                        # run_in_background=true — Agent sub-agents and
                        # Bash/PowerShell shells alike. The CLI puts this SAME id
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
                            and not parent_tool_id
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
                        elif tool_id and not parent_tool_id and block.name in ("Task", "Agent"):
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
                            and parent_tool_id
                            and (
                                tool_input.get("run_in_background")
                                or tool_input.get("runInBackground")
                            )
                        ):
                            # SUBAGENT-lane backgrounded launch (a grandchild).
                            # Recorded in a DEDICATED set — never
                            # mark_background_launch, which would pin the pool /
                            # chip it as the parent's own work — so the idle
                            # reader can still deliver its terminal notification
                            # instead of suppressing it as foreground and
                            # stranding a sub-agent parked awaiting the task
                            # (the top-level-only gate above cannot see it).
                            task_registry.mark_subagent_background_launch(session_id, tool_id)
                            # Link the child launch to its owner (the
                            # sub-agent's own launch id = parent_tool_id).
                            task_registry.link_child_to_owner(
                                session_id, tool_id, parent_tool_id
                            )
                            logger.info(
                                "task-launch-classification session=%s tool_id=%s "
                                "classification=background-flag-subagent-lane",
                                session_id, tool_id,
                            )
                        elif (
                            tool_id
                            and parent_tool_id
                            and block.name in _IMPLICIT_BACKGROUND_TOOL_NAMES
                        ):
                            # SUBAGENT-lane FLAG-LESS background builtin
                            # (Monitor): always spawns a background task but
                            # carries no run_in_background flag, so the branch
                            # above can never classify it. Same dedicated set as
                            # above — never mark_background_launch (no pool pin,
                            # no chip scope). TOP-LEVEL Monitors fall
                            # through unclassified on purpose: their wakes are
                            # CLI-owned (queue enqueue + self-dequeue) and the
                            # app must keep suppressing them.
                            task_registry.mark_subagent_background_launch(session_id, tool_id)
                            # Link the child launch to its owner (the
                            # sub-agent's own launch id = parent_tool_id).
                            task_registry.link_child_to_owner(
                                session_id, tool_id, parent_tool_id
                            )
                            logger.info(
                                "task-launch-classification session=%s tool_id=%s "
                                "classification=background-builtin-subagent-lane",
                                session_id, tool_id,
                            )

                        # Check if this is a subagent invocation ("Task" or "Agent")
                        if block.name in ("Task", "Agent"):
                            # Derive agent_type: prefer subagent_type, fall back to description
                            agent_type = (
                                tool_input.get("subagent_type")
                                or tool_input.get("description")
                                or "Agent"
                            )
                            # Shape gate: fluid's bare-named "Task" dispatch
                            # builtin always carries an "agent" key and never a
                            # "subagent_type" (fluid/.../builtins/task.py:
                            # {agent, prompt, mode, label}); the CLI's genuine
                            # Task/Agent subagent tools carry subagent_type /
                            # description / prompt and never "agent". An
                            # app-dispatched spawn is announced by
                            # WorkerFeed.started() with the REAL agent name and
                            # the worker session id as task_id (joined back here
                            # via launch_tool_use_id) — that row is canonical, so
                            # the generic pair emitted below would duplicate it
                            # in session_events.
                            is_app_dispatch = (
                                isinstance(tool_input, dict)
                                and "agent" in tool_input
                                and "subagent_type" not in tool_input
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

                            if is_app_dispatch:
                                # WorkerFeed owns the announcement for this spawn.
                                app_dispatch_tool_ids.add(tool_id)
                            else:
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
                            # A subagent's child tool (including one under a
                            # CROSS-TURN background parent seeded at turn start):
                            # it must NEVER reach the main-lane tool handler.
                            # Tool use from subagent - send as subagent_tool_use
                            yield {
                                "type": "subagent_tool_use",
                                "agent_id": parent_tool_id,
                                "name": block.name,
                                "id": tool_id,
                                "input": tool_input
                            }
                        else:
                            # Regular top-level tool
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

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            # The CLI (2.1.195+) launches Agent/Task subagents
                            # ASYNC WITHOUT run_in_background in the tool input — the
                            # launch ToolResult here is only the async launch ACK, so
                            # the input-flag arm above never fired and the launch gate
                            # is empty; every genuine agent completion would then be
                            # suppressed as foreground. Arm the launch gate from
                            # the ACK sniff for a TOP-LEVEL launch whose result reads
                            # like the async-launch placeholder. A synchronous
                            # sub-agent's full result text does NOT match the sniffer,
                            # so it is never armed — preserving foreground-suppression
                            # semantics.
                            _is_top_level_launch = (
                                tool_use_id
                                and not parent_tool_id
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
                                and parent_tool_id
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
                            # Defer-don't-terminate gate: emit ONLY on positive
                            # foreground evidence; DEFER when ambiguous (a reworded ACK
                            # must not fire a premature subagent_done).
                            # A launch-failure result (dispatch rejection / is_error) is
                            # positive terminal evidence — nothing spawned — so it settles
                            # immediately and is exempt from the defer policy.
                            _launch_error = _task_launch_failure_error(
                                output, bool(getattr(block, 'is_error', False))
                            )
                            _decision = (
                                "emit" if _launch_error is not None
                                else _should_defer_subagent_done(
                                    session_id, tool_use_id, task_info, output
                                )
                            )
                            if _decision == "emit":
                                if tool_use_id not in app_dispatch_tool_ids:
                                    # App-dispatched spawns settle via WorkerFeed's
                                    # canonical subagent_done (keyed on the worker
                                    # session id) — a generic terminal here would
                                    # duplicate it in session_events.
                                    yield {
                                        "type": "subagent_done",
                                        "tool_id": tool_use_id,
                                        "agent_id": tool_use_id,  # Use tool_id as agent_id
                                        "agent_type": task_info.get("agent_type", "unknown"),
                                        "result": _launch_error if _launch_error is not None else output,
                                        "is_error": True if _launch_error is not None else getattr(block, 'is_error', False)
                                    }
                                # Clean up tracking
                                del task_tool_uses[tool_use_id]
                            elif _decision == "defer":
                                # Park it; keep the node RUNNING and task_tool_uses
                                # intact.
                                deferred_subagent_done[tool_use_id] = {
                                    "output": output,
                                    "is_error": getattr(block, 'is_error', False),
                                    "agent_type": task_info.get("agent_type", "unknown"),
                                }
                        # Check if this tool result is from a subagent.
                        elif is_subagent_lane:
                            # Tool result from subagent - send as subagent_tool_result
                            yield {
                                "type": "subagent_tool_result",
                                "agent_id": parent_tool_id,
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": getattr(block, 'is_error', False),
                                # Inline image (browser/computer-use screenshot):
                                # the recorder content-addresses it and strips
                                # the raw data URI before the event log append.
                                **({"screenshot": screenshot} if screenshot else {}),
                            }
                        else:
                            # Regular top-level tool_result
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
                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "tool_use_id": tool_use_id,
                            "output": output,
                            "is_error": False,
                            "server_side": True,
                        }

                metadata["model"] = message.model

                # Live model_info note: re-emit when the MAIN-lane model the SDK
                # is actually running changes mid-turn (rerouting). Seeded from
                # the init frame above, so init + an identical first assistant
                # model collapse to one event; a subagent's own model (its
                # AssistantMessages carry parent_tool_use_id) never counts.
                if (
                    not is_subagent_lane
                    and message.model
                    and message.model != last_model_info
                ):
                    last_model_info = message.model
                    yield _model_info_frame(message.model)

                # Per-turn usage from AssistantMessage.
                # Gated on the MAIN lane, exactly like the model_info note above:
                # a subagent runs its own context window, so letting its usage
                # through would overwrite the parent session's last-call figures
                # (which _persist_turn_results writes to
                # sessions.last_input_tokens/…) with an unrelated number.
                if message.usage and not is_subagent_lane:
                    # Track per-API-call values (overwrite → last call wins).
                    # Used by _persist_turn_results as the real context-window
                    # size; ResultMessage.usage is cumulative and unusable here.
                    # A snapshot-only record describes a call that was already
                    # billed, so its zeroes must not blank the last-call size —
                    # but it still ships, to carry the breakdown it exists for.
                    if not _is_context_snapshot_only(message.usage):
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
                logger.debug(f"[WS] UserMessage received: content_type={type(message.content).__name__}, parent_tool_id={parent_tool_id}")

                # Suppress local command output from subagent UserMessages.
                is_subagent_user_msg = is_subagent_lane

                # Handle string content (rare but possible)
                if isinstance(message.content, str):
                    content = message.content
                    logger.debug(f"[WS] UserMessage string content preview: {content[:200] if content else 'empty'}")
                    # NOTE: a CLI-injected <task-notification> user-lane steering
                    # message can flow through here mid-turn. No latch is needed:
                    # the CLI's own Task/Agent tools are banned on the delegated
                    # leg, so no CLI notification duplicates an app unpark.
                    # At-rest injections never pass through stream_to_websocket.
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
                        # <task-notification> observed here. See the
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
                            # CLI built-in commands may return output without <local-command-stdout> tags
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

                        # Check if this is a result for a Task tool (subagent)
                        if tool_use_id in task_tool_uses:
                            task_info = task_tool_uses[tool_use_id]
                            # UserMessage lane: the CLI (2.1.195+) launches
                            # Agent/Task subagents ASYNC without run_in_background in
                            # the tool input — the launch ToolResult here is only the
                            # async launch ACK, so the input-flag arm never fired and
                            # every genuine agent completion would be suppressed as
                            # foreground. Arm the launch gate from the ACK sniff for
                            # a TOP-LEVEL launch. A synchronous sub-agent's full result
                            # text does NOT match the sniffer, so it is never armed —
                            # preserving foreground semantics.
                            _is_top_level_launch = (
                                tool_use_id
                                and not parent_tool_id
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
                                and parent_tool_id
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
                            # via parent_tool_use_id and it completes on the terminal
                            # TaskNotificationMessage. Do NOT emit subagent_done / delete
                            # tracking, or the node renders "completed / no activity".
                            # Defer-don't-terminate gate (UserMessage lane): emit
                            # ONLY on positive foreground evidence; DEFER when ambiguous.
                            # A launch-failure result (dispatch rejection / is_error) is
                            # positive terminal evidence — nothing spawned — so it settles
                            # immediately and is exempt from the defer policy.
                            _launch_error = _task_launch_failure_error(
                                output, bool(block.is_error)
                            )
                            _decision = (
                                "emit" if _launch_error is not None
                                else _should_defer_subagent_done(
                                    session_id, tool_use_id, task_info, output
                                )
                            )
                            if _decision == "emit":
                                if tool_use_id not in app_dispatch_tool_ids:
                                    # See the AssistantMessage-lane sibling: an
                                    # app-dispatched spawn settles via WorkerFeed's
                                    # canonical subagent_done, never here.
                                    yield {
                                        "type": "subagent_done",
                                        "tool_id": tool_use_id,
                                        "agent_id": tool_use_id,  # Use tool_id as agent_id
                                        "agent_type": task_info.get("agent_type", "unknown"),
                                        "result": _launch_error if _launch_error is not None else output,
                                        "is_error": True if _launch_error is not None else block.is_error
                                    }
                                # Clean up tracking
                                del task_tool_uses[tool_use_id]
                            elif _decision == "defer":
                                # Park it; keep the node RUNNING and task_tool_uses
                                # intact.
                                deferred_subagent_done[tool_use_id] = {
                                    "output": output,
                                    "is_error": block.is_error,
                                    "agent_type": task_info.get("agent_type", "unknown"),
                                }
                        # Check if this tool result is from a subagent.
                        elif is_subagent_lane:
                            # Tool result from subagent - send as subagent_tool_result
                            yield {
                                "type": "subagent_tool_result",
                                "agent_id": parent_tool_id,
                                "name": tool_name,
                                "tool_use_id": tool_use_id,
                                "output": output,
                                "is_error": block.is_error,
                                # Inline image (browser/computer-use screenshot):
                                # the recorder content-addresses it and strips
                                # the raw data URI before the event log append.
                                **({"screenshot": screenshot_b64} if screenshot_b64 else {}),
                            }
                        else:
                            # Regular top-level tool_result
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
                # this frame — the event log never carries rate_limit. They see
                # the change only via the shared usage_snapshots row + cluster
                # broadcast we persist below.
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
                # Terminal reason + typed per-model usage pass-through
                metadata["terminal_reason"] = getattr(message, "terminal_reason", None)
                metadata["model_usage"] = getattr(message, "model_usage", None)
                # Surface provider-reported errors
                result_errors = getattr(message, 'errors', None)
                if result_errors:
                    metadata["errors"] = result_errors
                    logger.warning(f"[WS] ResultMessage contains errors: {result_errors}")
                # Surface HTTP error status code; emit before result frame
                api_err = getattr(message, "api_error_status", None)
                if api_err is not None:
                    metadata["api_error_status"] = api_err
                    yield {"type": "api_error", "status": api_err, "session_id": session_id}
                    # Funnel through the unified notification system (persist + the
                    # bell + all the user's devices). The frontend `api_error`
                    # handler does not emit separately — the `notification` event does.
                    await _emit_api_error_notification(
                        api_user_id, session_id, api_err,
                        delegated=_adapter_is_delegated(state.adapter),
                    )
                # Extract token counts from usage dict
                if message.usage:
                    metadata["tokens_in"] = message.usage.get("input_tokens", 0)
                    metadata["tokens_out"] = message.usage.get("output_tokens", 0)
                    metadata["cache_creation_tokens"] = message.usage.get("cache_creation_input_tokens", 0)
                    metadata["cache_read_tokens"] = message.usage.get("cache_read_input_tokens", 0)
                    # Any call this turn with harness-estimated token counts
                    # (the adapter stamps the terminal usage); booked as a
                    # flag on the ledger row, never subtracted.
                    metadata["usage_estimated"] = bool(message.usage.get("usage_estimated"))

            else:
                logger.warning(f"[WS] Unhandled message type: {type(message).__name__}")

        # Log streaming performance summary
        _stream_elapsed = time.monotonic() - _stream_start
        logger.info(f"[WS] Streaming complete for session {session_id}: {_event_count} events in {_stream_elapsed:.1f}s")

        # Check if the loop ended due to an interrupt request
        # (receive_events() ended naturally after the adapter's interrupt())
        if state.interrupt_requested:
            interrupted = True
            logger.debug(f"[WS] Streaming loop ended after interrupt for session {session_id}")

        # End-of-turn drain flush (abort-safe). Runs ONLY on a
        # clean, NON-interrupted loop exit: it lives inside the streaming `try`,
        # so any exception/CancelledError jumps past it, and the `not interrupted`
        # guard drops a soft-interrupt (loop ended naturally after the adapter's
        # interrupt()). On any aborted path the parked entries are dropped
        # WITHOUT emitting — a stuck-running node is recoverable (FE re-open belt,
        # registry drain chip); a false terminal is not.
        #
        # A surviving parked entry is flushed as subagent_done ONLY when it is a
        # genuine SYNCHRONOUS FOREGROUND launch: its tool_use_id never saw an
        # in-stream TaskStarted (task_started_tool_use_ids) AND it is not in any
        # background set. An entry that DID see TaskStarted (dropped already at the
        # branch above) or that a late frame revealed as background settles on its
        # terminal TaskNotificationMessage instead — never here.
        if not interrupted and deferred_subagent_done:
            for _tuid, _parked in list(deferred_subagent_done.items()):
                if _tuid in task_started_tool_use_ids:
                    continue
                if session_id and (
                    task_registry.was_background_launch(session_id, _tuid)
                    or task_registry.was_subagent_background_launch(session_id, _tuid)
                    or task_registry.running_background_task_for_tool_use(session_id, _tuid)
                ):
                    continue
                if _tuid not in app_dispatch_tool_ids:
                    # App-dispatched spawn: WorkerFeed already emitted (or will
                    # emit) the canonical terminal keyed on the worker session id.
                    yield {
                        "type": "subagent_done",
                        "tool_id": _tuid,
                        "agent_id": _tuid,
                        "agent_type": _parked.get("agent_type", "unknown"),
                        "result": _parked.get("output"),
                        "is_error": _parked.get("is_error", False),
                    }
                task_tool_uses.pop(_tuid, None)
        deferred_subagent_done.clear()

        # Final drain of stderr queue after streaming loop completes
        for stderr_msg in await drain_stderr_queue():
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
        # sdk_session_id link are written even on a mid-turn crash. A bare
        # `return` here would skip all persistence and orphan the session on an
        # OOM/SIGKILL crash (sdk_session_id stays NULL → blank history). The
        # `crashed` flag suppresses the trailing `done`
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
    await _persist_turn_results(
        session_id=session_id,
        sdk_session_id=sdk_session_id,
        metadata=metadata,
        profile_id=profile_id,
        interrupted=interrupted,
        log_prefix="[WS] ",
    )

    # Capture the context-usage breakdown. Runs DETACHED and self-guards on
    # the pool's live adapter (a no-op if the subprocess died mid-turn), so it
    # never delays the terminal event below.
    schedule_context_usage_capture(session_id, log_prefix="[WS] ")

    # Push any agent-memory the CLI wrote this turn up to the shared DB.
    await push_memory(
        project_id, getattr(options, "cwd", None), memory_leg_for_options(options)
    )

    # For slash commands that produced no visible content, yield a notification
    # CLI built-in commands like /memory, /cost, /model execute inside the subprocess
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
            "message": "Query was interrupted",
            "metadata": {"terminal_reason": metadata.get("terminal_reason")}
        }
    elif metadata.get("is_error") and (
        not has_visible_content
        or (isinstance(metadata.get("errors"), list) and metadata.get("errors"))
    ):
        # An is_error result that carries error messages is a failed turn
        # whether or not content was rendered first. The fluid legs deliver
        # every provider failure this way — an ErrorEvent becomes a terminal
        # ResultMessage with `errors`, the generator ends cleanly, and
        # `crashed` never flips — so anything already on screen (a retry note,
        # a model banner, a tool call) flips `has_visible_content`; treating
        # that as success would downgrade the failure to `done` and hide it from
        # the transcript, the sidebar, and the retry card. Surface the terminal `error` frame
        # instead: the frontend shows the banner and resets streaming state.
        _errs = metadata.get("errors")
        _msg = (
            "; ".join(str(e) for e in _errs)
            if isinstance(_errs, list) and _errs
            else "The provider returned an error before producing any output."
        )
        yield {"type": "error", "message": _msg, "session_id": session_id}
    else:
        yield {
            "type": "done",
            "session_id": session_id,
            "metadata": metadata
        }

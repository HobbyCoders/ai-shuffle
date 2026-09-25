"""
REST API for slash commands and rewind operations.

Provides endpoints for:
- Listing available commands (for autocomplete)
- Getting command details
- Rewind operations (list checkpoints, execute rewind)

Rewind uses persisted transcript/checkpoint services rather than terminal parsing.
"""

import asyncio
import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth
from app.api.sessions import check_session_access
from app.api.common import resolve_project_path
from app.api.command_context import command_context, command_provider_kind, command_setting_sources
from app.core.slash_commands import (
    get_command_by_name, get_all_commands, is_interactive_command,
)
# Legacy JSONL fallback and checkpoint-backed rewind services.
from app.core.jsonl_rewind import jsonl_rewind_service
from app.core.pg_session_store import (
    RewindTargetNotFound,
    main_transcript_is_empty,
    truncate_session_store_to_checkpoint,
)
from app.core.rewind_db_sync import (
    resolve_rewind_anchor,
    apply_rewind_delete,
    delete_all_session_rows,
)
from app.core.jsonl_parser import invalidate_parse_cache, _is_task_notification
from app.core.sync_engine import sync_engine
from app.core.session_pool import get_session_pool
from app.core.models import (
    RewindRequest, RewindCheckpoint, RewindCheckpointsResponse,
    RewindExecuteResponse
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/commands",
    tags=["Commands"],
    dependencies=[Depends(require_auth)],
)


class CommandInfo(BaseModel):
    """Command information for API responses"""
    name: str
    display: str
    description: str
    argument_hint: Optional[str] = None
    type: str  # custom, sdk_builtin, plugin, skill, or interactive
    source: Optional[str] = None  # "project" or "user" for custom commands
    namespace: Optional[str] = None


class CommandListResponse(BaseModel):
    """Response for command list endpoint"""
    commands: List[CommandInfo]
    count: int


class CommandDetailResponse(BaseModel):
    """Detailed command information"""
    name: str
    display: str
    description: str
    content: Optional[str] = None  # Prompt content (only for custom commands)
    argument_hint: Optional[str] = None
    type: str
    source: Optional[str] = None
    namespace: Optional[str] = None
    allowed_tools: List[str] = []
    model: Optional[str] = None
    is_interactive: bool = False


def get_working_dir_for_project(
    project_id: Optional[str],
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Get the working directory for a project, its worktree, or a v2 branch
    session's own checkout (sessions.branch + project_v2_id).
    """
    if not worktree_id and session_id:
        from app.core.project_resolver import resolve_existing_cwd
        from app.db import database

        session = database.get_session(session_id)
        if session:
            cwd = resolve_existing_cwd(session)
            if cwd:
                return cwd
    result = resolve_project_path(
        project_id,
        check_access=False,
        worktree_id=worktree_id,
        fallback_to_workspace=True,
    )
    return str(result)


@router.get("/", response_model=CommandListResponse)
async def list_commands(
    project_id: Optional[str] = None,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    profile_id: Optional[str] = None,
    mode: Optional[str] = None,
    http_request: Request = None,
):
    """
    List all available slash commands.

    Includes discovered custom/plugin/skill commands and the noninteractive
    built-ins supported by the selected runtime. Does not connect to a model.
    """
    def _list():
        working_dir, session = command_context(http_request, project_id, worktree_id, session_id, profile_id)
        kind = command_provider_kind(http_request, session, model, profile_id, mode)
        sources = command_setting_sources(http_request, session, profile_id)
        return get_all_commands(working_dir, provider_kind=kind, setting_sources=sources)

    commands = await run_sync(_list)

    return CommandListResponse(
        commands=[CommandInfo(**cmd) for cmd in commands],
        count=len(commands)
    )


@router.get("/{command_name}", response_model=CommandDetailResponse)
async def get_command(
    command_name: str,
    project_id: Optional[str] = None,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    profile_id: Optional[str] = None,
    mode: Optional[str] = None,
    http_request: Request = None,
):
    """
    Get detailed information about a specific command.

    The command_name should not include the leading slash.
    """
    working_dir, session = await run_sync(
        command_context, http_request, project_id, worktree_id, session_id, profile_id
    )
    kind = await run_sync(command_provider_kind, http_request, session, model, profile_id, mode)
    sources = await run_sync(command_setting_sources, http_request, session, profile_id)
    # Check if it's an interactive command
    if kind == "delegated" and is_interactive_command(command_name):
        from app.core.slash_commands import INTERACTIVE_COMMANDS
        info = INTERACTIVE_COMMANDS.get(command_name)
        if info:
            return CommandDetailResponse(
                name=command_name,
                display=f"/{command_name}",
                description=info["description"],
                type="interactive",
                is_interactive=True
            )

    # Look for custom command
    cmd = await run_sync(
        get_command_by_name, working_dir, command_name,
        provider_kind=kind, setting_sources=sources,
    )

    if not cmd:
        entries = await run_sync(get_all_commands, working_dir, provider_kind=kind, setting_sources=sources)
        builtin = next((entry for entry in entries if entry["name"] == command_name and entry["type"] == "sdk_builtin"), None)
        if builtin:
            return CommandDetailResponse(**builtin)
        raise HTTPException(status_code=404, detail=f"Command not found: {command_name}")

    return CommandDetailResponse(
        name=cmd.name,
        display=cmd.get_display_name(),
        description=cmd.description,
        content=cmd.content,
        argument_hint=cmd.argument_hint,
        type=getattr(cmd, "type", "custom"),
        source=cmd.source,
        namespace=cmd.namespace,
        allowed_tools=cmd.allowed_tools,
        model=cmd.model,
        is_interactive=False
    )


# =============================================================================
# Rewind API V2 - Direct JSONL manipulation (bulletproof)
# =============================================================================

class CheckpointV2(BaseModel):
    """V2 Checkpoint model for rewind operations"""
    uuid: str
    index: int
    message_preview: str
    full_message: str
    role: str = "user"  # "user" or "assistant"
    timestamp: Optional[str] = None


class CheckpointsResponseV2(BaseModel):
    """V2 Response containing available checkpoints"""
    success: bool
    session_id: str
    sdk_session_id: Optional[str] = None
    checkpoints: List[CheckpointV2] = []
    error: Optional[str] = None


class RewindRequestV2(BaseModel):
    """V2 Rewind request using message UUID instead of index"""
    target_uuid: str = ""  # UUID of the message to rewind to ("" when using target_content)
    restore_chat: bool = True  # Truncate JSONL (rewind conversation)
    restore_files: bool = False  # Not supported: reported as an error; the chat rewind still runs
    include_response: bool = True  # Keep Claude's response to target message
    target_role: str = "user"  # Role of target message ("user" or "assistant")
    # v2 event-log affordance: a live v2 message rendered from the event log may
    # have no transcript uuid yet. The client then sends the target's content
    # plus its 0-based occurrence among the client's IDENTICAL-content user
    # prompts; the server resolves the checkpoint uuid by content match against
    # the JSONL checkpoint list and proceeds through the normal uuid flow.
    # (Content anchoring, not a global ordinal: the SDK transcript contains
    # user-role lines the client never renders — synthetic "[Request
    # interrupted by user]" markers, task notifications, prompts retained
    # after an empty-turn discard — so global positions skew in any session
    # that has seen an interrupt.)
    target_occurrence: Optional[int] = None
    target_content: Optional[str] = None


class RewindResponseV2(BaseModel):
    """V2 Rewind response with detailed results"""
    success: bool
    message: str
    chat_rewound: bool = False
    files_rewound: bool = False
    messages_removed: int = 0
    error: Optional[str] = None
    partial_success: bool = False  # True if chat rewound but files didn't (when files were requested)
    warning: Optional[str] = None  # Warning message for partial success scenarios


def _get_checkpoints(sdk_session_id: str, working_dir: str):
    """Checkpoints for a session, from whichever transcript store owns it.

    Store-aware read facade. A fluid session keeps its transcript in fluid's
    node tree (``fluid_session_nodes``) and its ``sdk_session_id`` is a fluid
    ``ses_...`` id, so the JSONL service finds nothing for it — which made the
    rewind content guard fail closed ("transcript has 0 user prompt(s)
    matching..."). ``is_fluid_session`` is prefix-gated, so a pre-fluid session
    never even queries: this is a no-op on the legacy path.
    """
    from app.core.provider.fluid_transcript import get_fluid_checkpoints, is_fluid_session

    if is_fluid_session(sdk_session_id):
        return get_fluid_checkpoints(sdk_session_id)
    return jsonl_rewind_service.get_checkpoints(sdk_session_id, working_dir)


@router.get("/rewind/checkpoints/{session_id}")
async def get_rewind_checkpoints(session_id: str, http_request: Request = None):
    """
    Get available checkpoints for a session that can be rewound to.

    Uses JSONL rewind service to parse checkpoints directly from JSONL file.
    """
    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if http_request is not None:
        check_session_access(http_request, session)
    from app.api.rewind_owner import forward_rewind
    remote = await forward_rewind(session, "checkpoints")
    if remote is not None:
        return CheckpointsResponseV2.model_validate(remote)

    def _load_checkpoints() -> CheckpointsResponseV2:
        """Keep local transcript/checkpoint IO in one worker-thread hop."""
        sdk_session_id = session.get("sdk_session_id")
        if not sdk_session_id:
            return CheckpointsResponseV2(
                success=False,
                session_id=session_id,
                checkpoints=[],
                error="Session has no SDK session ID - start a conversation first"
            )

        # Get working directory for project
        working_dir = get_working_dir_for_project(session.get("project_id"))

        # Get checkpoints from JSONL rewind service
        checkpoints = _get_checkpoints(sdk_session_id, working_dir)

        if not checkpoints:
            # Fallback: get from our local database (less accurate but better than nothing)
            messages = database.get_session_messages(session_id)
            fallback_checkpoints = []

            for i, msg in enumerate(messages):
                if msg["role"] == "user":
                    content = msg["content"]
                    fallback_checkpoints.append(CheckpointV2(
                        uuid=f"db-{msg['id']}",  # Prefix to indicate DB-sourced
                        index=len(fallback_checkpoints),
                        message_preview=content[:100] + ('...' if len(content) > 100 else ''),
                        full_message=content,
                        timestamp=str(msg.get("created_at", "")),
                    ))

            return CheckpointsResponseV2(
                success=True,
                session_id=session_id,
                sdk_session_id=sdk_session_id,
                checkpoints=fallback_checkpoints,
                error="Using fallback - JSONL file not found" if not fallback_checkpoints else None
            )

        # Convert to response model
        response_checkpoints = [
            CheckpointV2(
                uuid=cp.uuid,
                index=cp.index,
                message_preview=cp.message_preview,
                full_message=cp.full_message,
                role=cp.role,
                timestamp=cp.timestamp,
            )
            for cp in checkpoints
        ]

        return CheckpointsResponseV2(
            success=True,
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            checkpoints=response_checkpoints
        )

    return await run_sync(_load_checkpoints)


# ----------------------------------------------------------------------
# v2 event-log rewind resolution (transcript.rewound)
# ----------------------------------------------------------------------

def _load_message_events(session_id: str) -> List[dict]:
    """All message.user / message.assistant rows of the event log, seq order.

    Rows logically erased by a later ``transcript.rewound`` (seq >= its
    from_seq) are dropped, mirroring the client reducer — resolution must run
    against the transcript the client actually renders.
    """
    from app.db import session_events as session_events_db

    rows: List[dict] = []
    cursor = 0
    while True:
        page = session_events_db.get_events(session_id, from_seq=cursor, limit=500)
        if not page:
            break
        for row in page:
            cursor = row["seq"]
            if row["event_type"] in ("message.user", "message.assistant"):
                rows.append(row)
            elif row["event_type"] == "transcript.rewound":
                from_seq = (row.get("payload") or {}).get("from_seq")
                if isinstance(from_seq, int):
                    rows = [r for r in rows if r["seq"] < from_seq]
        if len(page) < 500:
            break
    return rows


def _is_prompt_event(row: dict) -> bool:
    """True for a message.user event that is a genuine typed prompt.

    Command prompts (kind='command') are excluded because the JSONL checkpoint
    list (`get_checkpoints`) skips command/meta entries, and ordinals must be
    computed against the SAME population on both sides.

    Task-notification wake prompts (synthetic user rows containing a
    ``<task-notification>`` block) are likewise excluded: the UI
    renders them as system Task cards, not user bubbles, so they must never
    resolve as rewind checkpoints. Reuses ``jsonl_parser._is_task_notification``
    — the shared ``<task-notification>`` substring convention used across
    history_backfill and the sessions.py orphan-turn guards.
    """
    if row["event_type"] != "message.user":
        return False
    payload = row.get("payload") or {}
    if payload.get("kind") == "command":
        return False
    if _is_task_notification(payload.get("content") or ""):
        return False
    return True


def _resolve_checkpoint_uuid_from_content(
    sdk_session_id: str,
    working_dir: str,
    target_content: Optional[str],
    target_occurrence: int,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a checkpoint uuid from the target prompt's content.

    Returns ``(uuid, error)`` — exactly one is non-None. The target is the
    ``target_occurrence``-th user checkpoint whose (trimmed) text equals
    ``target_content``. Content anchoring instead of a global ordinal: the
    SDK transcript contains user-role lines the client never renders
    (synthetic "[Request interrupted by user]" markers, task notifications,
    prompts retained after an empty-turn discard), so global positions skew
    in any session that has seen an interrupt — but those lines never equal
    a real prompt's text. FAILS CLOSED when nothing matches (content guard).
    """
    want = (target_content or "").strip()
    if not want:
        return None, (
            "Rewind refused: no target content supplied to resolve against "
            "(content guard)"
        )
    checkpoints = _get_checkpoints(sdk_session_id, working_dir)
    matches = [
        cp for cp in checkpoints
        if cp.role == "user" and (cp.full_message or "").strip() == want
    ]
    if target_occurrence < 0 or target_occurrence >= len(matches):
        return None, (
            f"Rewind refused: transcript has {len(matches)} user prompt(s) "
            f"matching the target content, occurrence {target_occurrence} "
            f"requested (content guard)"
        )
    cp = matches[target_occurrence]
    if not cp.uuid:
        return None, "Rewind refused: the matched checkpoint has no uuid"
    return cp.uuid, None


def _resolve_rewind_from_seq(
    session_id: str,
    sdk_session_id: str,
    working_dir: str,
    target_uuid: str,
    include_response: bool,
    target_role: str,
) -> tuple[Optional[int], Optional[str]]:
    """Resolve the event-log seq a chat rewind truncates from. FAIL CLOSED.

    Returns ``(from_seq, error)``:
    - ``(seq, None)``   — append ``transcript.rewound(from_seq=seq)`` on success.
    - ``(None, None)``  — the target is already the transcript tail (nothing
      after it to drop) — the rewind proceeds but no event is appended.
    - ``(None, error)`` — resolution failed; the caller must refuse the ENTIRE
      rewind before truncating anything, or the event log and the truncated
      transcript would disagree forever.

    Resolution order:
    1. uuid short-circuit — a backfilled (or uuid-stamped) message.* event whose
       payload uuid equals the target.
    2. content + occurrence — the target checkpoint's text and its occurrence
       among identical-text JSONL user checkpoints map to the matching
       prompt-shaped message.user event. Content anchoring (not a global
       ordinal) because the JSONL contains user-role lines the event log
       never records — synthetic interrupt markers, retained discards.
    """
    events = _load_message_events(session_id)
    if not events:
        return None, (
            "Rewind refused: the v2 event log has no message events to resolve "
            "the target against"
        )

    target_idx: Optional[int] = None
    for i, row in enumerate(events):
        if (row.get("payload") or {}).get("uuid") == target_uuid:
            target_idx = i
            break

    if target_idx is None:
        if target_role != "user":
            return None, (
                f"Rewind refused: assistant target {target_uuid} not found in "
                f"the event log (assistant targets resolve by uuid only)"
            )
        checkpoints = _get_checkpoints(sdk_session_id, working_dir)
        user_cps = [cp for cp in checkpoints if cp.role == "user"]
        pos = next(
            (k for k, cp in enumerate(user_cps) if cp.uuid == target_uuid), None
        )
        if pos is None:
            return None, (
                f"Rewind refused: target {target_uuid} not found among the "
                f"transcript's user prompts"
            )
        want = (user_cps[pos].full_message or "").strip()
        occurrence = sum(
            1 for cp in user_cps[:pos]
            if (cp.full_message or "").strip() == want
        )
        prompts = [
            (i, row) for i, row in enumerate(events)
            if _is_prompt_event(row)
            and ((row.get("payload") or {}).get("content") or "").strip() == want
        ]
        if occurrence >= len(prompts):
            return None, (
                f"Rewind refused: event log has {len(prompts)} user prompt(s) "
                f"matching the target's text but the transcript places it at "
                f"occurrence {occurrence} (content guard)"
            )
        target_idx = prompts[occurrence][0]

    if target_role == "assistant" or include_response:
        # Keep the target (and its response); drop from the NEXT user prompt.
        # Commands count as a boundary here — mirroring _next_real_user_index,
        # which cuts at any genuine user entry.
        for row in events[target_idx + 1:]:
            if row["event_type"] == "message.user":
                return int(row["seq"]), None
        return None, None  # already the tail — nothing to drop
    return int(events[target_idx]["seq"]), None


@router.post("/rewind/execute/{session_id}")
async def execute_rewind(session_id: str, request: RewindRequestV2, http_request: Request = None):
    """
    Execute a rewind operation to restore conversation and optionally files.

    Direct JSONL truncation - bulletproof, no PTY/terminal needed.

    How it works:
    1. Truncates the JSONL file at the target message UUID
    2. Refuses file restore (file checkpoints are not supported)
    3. Syncs our local database
    4. The next resume uses the truncated context

    Options:
    - restore_chat: Truncate JSONL (rewind conversation context)
    - restore_files: Not supported; reported as an error while the chat rewind still runs
    - include_response: Keep Claude's response to target message
    """
    # Authorize and route before inspecting local provider state or resolving a
    # checkout. A mirrored session intentionally has no local resume pointer.
    def _load_session():
        session = database.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if http_request is not None:
            check_session_access(http_request, session)

        return session

    session = await run_sync(_load_session)
    from app.api.rewind_owner import forward_rewind
    remote = await forward_rewind(session, "execute", request.model_dump())
    if remote is not None:
        return RewindResponseV2.model_validate(remote)

    working_dir = None
    if session.get("sdk_session_id") and not request.target_uuid.startswith("db-"):
        working_dir = await run_sync(get_working_dir_for_project, session.get("project_id"))

    sdk_session_id = session.get("sdk_session_id")
    if not sdk_session_id:
        return RewindResponseV2(
            success=False,
            message="Cannot execute rewind",
            error="Session has no SDK session ID"
        )

    # Handle DB-sourced UUIDs (fallback case)
    if request.target_uuid.startswith("db-"):
        return RewindResponseV2(
            success=False,
            message="Cannot rewind to database checkpoint",
            error="JSONL file not found - rewind requires the original JSONL file"
        )

    # Which transcript store owns this session? A fluid (flag-on) session keeps
    # its conversation in fluid's node tree, so the JSONL/session-store
    # truncation path below simply has nothing to cut — it needs the node-tree
    # rewind instead. Prefix-gated probe: a legacy session never queries.
    from app.core.provider.fluid_transcript import is_fluid_session
    fluid_session = await asyncio.to_thread(is_fluid_session, sdk_session_id)

    logger.info(
        f"Rewind requested: session={session_id}, sdk_session={sdk_session_id}, "
        f"target_uuid={request.target_uuid}, target_role={request.target_role}, "
        f"restore_chat={request.restore_chat}, restore_files={request.restore_files}, "
        f"include_response={request.include_response}, "
        f"target_occurrence={request.target_occurrence}"
    )

    # --- v2 event log: resolve the transcript.rewound position UP FRONT ---
    # For v2_stream sessions the append-only event log is a transcript replica;
    # a chat rewind must append transcript.rewound(from_seq) or replays keep
    # showing the rewound-away messages forever. Resolution is FAIL CLOSED and
    # happens BEFORE anything is truncated: if we cannot place the target in
    # the log, we refuse the whole rewind rather than desync the two stores.
    v2_session = bool(session.get("v2_stream"))
    rewound_from_seq: Optional[int] = None
    if v2_session and request.restore_chat:
        from app.core.history_backfill import ensure_backfilled
        await ensure_backfilled(session_id)

        if not request.target_uuid:
            # Content affordance: a live v2 message has no transcript uuid yet.
            if request.target_content is None or request.target_occurrence is None:
                return RewindResponseV2(
                    success=False,
                    message="Cannot execute rewind",
                    error=(
                        "No rewind target: target_uuid or "
                        "target_content+target_occurrence is required"
                    ),
                )
            resolved_uuid, content_err = await asyncio.to_thread(
                _resolve_checkpoint_uuid_from_content,
                sdk_session_id, working_dir,
                request.target_content, request.target_occurrence,
            )
            if resolved_uuid is None:
                return RewindResponseV2(
                    success=False,
                    message="Rewind refused",
                    error=content_err,
                )
            request.target_uuid = resolved_uuid

        rewound_from_seq, resolve_err = await asyncio.to_thread(
            _resolve_rewind_from_seq,
            session_id, sdk_session_id, working_dir,
            request.target_uuid, request.include_response, request.target_role,
        )
        if resolve_err is not None:
            return RewindResponseV2(
                success=False,
                message="Rewind refused",
                error=resolve_err,
            )
    elif not request.target_uuid:
        return RewindResponseV2(
            success=False,
            message="Cannot execute rewind",
            error="No rewind target: target_uuid is required",
        )

    # Track what was rewound
    chat_rewound = False
    files_rewound = False
    messages_removed = 0
    errors = []
    # Set by the fluid branch below: the outcome of re-seeding the
    # delegated CLI from fluid's post-rewind context. None = never attempted.
    reseed_result = None

    # CRITICAL: evict any live pooled CLI subprocess for this session BEFORE we
    # touch the transcript. A pooled adapter's CLI subprocess keeps the JSONL
    # file open and appends to it. If we truncate/rewrite the file underneath
    # a live process, its next write lands past the new (shorter) EOF — leaving a
    # gap of NUL bytes — or flushes a stale buffer, producing duplicate/partial
    # lines. That corrupts the JSONL and breaks the next resume. Removing the
    # pool entry first guarantees the transcript is quiescent; the next turn
    # resumes a fresh client from the truncated file. Best-effort: an eviction hiccup must not
    # abort a rewind the user explicitly requested.
    if request.restore_chat or request.restore_files:
        try:
            pool = get_session_pool()
            if pool.contains(session_id):
                await pool.remove(session_id)
                logger.info(
                    f"Rewind evicted live pooled client for session {session_id} "
                    f"before mutating transcript {sdk_session_id}"
                )
        except Exception as evict_err:
            logger.error(
                f"Rewind pool eviction failed for session {session_id}: {evict_err}",
                exc_info=True,
            )

    # File restore is not supported. It relied on the Agent SDK resuming the
    # session to replay file checkpoints, and nothing drives the CLI through the
    # SDK any more. Refuse for every session so the chat rewind below still runs
    # (partial success) and the user gets a clear reason.
    if request.restore_files:
        errors.append(
            "File restore is not available (file checkpoints are not supported)"
        )

    # Chat rewind, fluid (flag-on) sessions. The transcript is fluid's node tree,
    # so the rewind is fluid's NATIVE one: move ``fluid_sessions.head_node_id``
    # back up the parent chain. Nothing is deleted — the rewound-away nodes stay
    # in the tree but off-path, so ``load_path`` (and therefore the context the
    # next turn builds) stops at the checkpoint and that turn branches a sibling.
    # WHERE it cuts comes from the same ``compute_rewind_keep_count`` the legacy
    # truncation uses, so the rewind lands on the same message either way.
    #
    # The legacy ``session_messages`` rows are truncated alongside it (same
    # anchor logic as below) because they are what the UI actually renders for
    # these sessions today — omitting that is the "rewind succeeded but the
    # message is still in chat" bug.
    if request.restore_chat and fluid_session:
        from app.core.provider.fluid_transcript import (
            rewind_fluid_session,
            reseed_delegated_cli_session,
        )

        anchor_id: Optional[int] = None
        try:
            anchor_id = await asyncio.to_thread(
                resolve_rewind_anchor,
                session_id=session_id,
                sdk_session_id=sdk_session_id,
                working_dir=working_dir,
                target_uuid=request.target_uuid,
            )
        except Exception as anchor_err:
            logger.error(
                f"Fluid rewind DB anchor resolution failed for session "
                f"{session_id}: {anchor_err}",
                exc_info=True,
            )

        fluid_result = await asyncio.to_thread(
            rewind_fluid_session,
            sdk_session_id,
            request.target_uuid,
            request.include_response,
            request.target_role,
        )
        if fluid_result.success:
            chat_rewound = True
            messages_removed = fluid_result.messages_removed or 0
            if fluid_result.remaining_user_turns is not None:
                turn_diff = fluid_result.remaining_user_turns - session.get("turn_count", 0)
                if turn_diff != 0:
                    await asyncio.to_thread(
                        database.update_session, session_id, turn_increment=turn_diff
                    )
        else:
            errors.append(f"Chat rewind failed: {fluid_result.error}")

        if chat_rewound:
            if anchor_id is not None:
                try:
                    db_removed = await asyncio.to_thread(
                        apply_rewind_delete,
                        session_id=session_id,
                        anchor_id=anchor_id,
                        include_response=request.include_response,
                    )
                    logger.info(
                        f"Fluid rewind DB sync: session={session_id}, "
                        f"target_node={request.target_uuid}, "
                        f"db_rows_removed={db_removed}, "
                        f"nodes_dropped={messages_removed}"
                    )
                except Exception as db_err:
                    logger.error(
                        f"Fluid rewind DB sync failed for session {session_id}: {db_err}",
                        exc_info=True,
                    )
                    errors.append(f"DB sync failed: {db_err}")
            else:
                logger.warning(
                    f"Fluid rewind DB sync skipped: no anchor row resolved for "
                    f"session={session_id} target={request.target_uuid}"
                )
            # Fluid owns the context, so the DELEGATED (claude CLI)
            # engine must not be left resuming its own pre-rewind history.
            # Materialize the post-rewind path as a CLI session file under a
            # fresh uuid and store it as the session's --resume handle; the
            # adapter's _resolve_cli_session_id picks it up on the next turn
            # with no further plumbing. A no-op for native-engine sessions
            # (nothing stored to re-seed). Best-effort: a failure downgrades to
            # the divergence warning below, it never fails the rewind.
            reseed_result = await asyncio.to_thread(
                reseed_delegated_cli_session, sdk_session_id, working_dir
            )
            logger.info(
                f"Fluid rewind CLI re-seed: session={session_id}, "
                f"status={reseed_result.status}, "
                f"entries={reseed_result.entries_written}, "
                f"error={reseed_result.error}"
            )

            # The sdk_session_id pointer is NOT cleared even when the rewind
            # emptied the transcript: a fluid session with a NULL head is a
            # perfectly resumable session (the next turn appends a root node),
            # unlike a CLI session id whose transcript no longer exists.
            invalidate_parse_cache(sdk_session_id)

    # Execute chat rewind. There are up to three storage layers to keep in sync:
    #   (a) the Postgres session store (``session_store_entries``) — the source
    #       of truth the UI reads,
    #   (b) the on-disk JSONL transcript the SDK resumes from, and
    #   (c) the legacy ``session_messages`` rows.
    elif request.restore_chat:
        # (c) Resolve the legacy-DB anchor BEFORE truncating anything. The
        # fallback content-matcher needs the target uuid's text from a still-
        # intact transcript — the JSONL when present, else the session store
        # (which the next step truncates). This also runs the uuid backfill so
        # subsequent rewinds hit the fast path.
        anchor_id: Optional[int] = None
        try:
            # Blocking: parses the full transcript (file IO / store read) and
            # hits the DB — off the event loop, or every other request stalls
            # for the duration on large sessions.
            anchor_id = await asyncio.to_thread(
                resolve_rewind_anchor,
                session_id=session_id,
                sdk_session_id=sdk_session_id,
                working_dir=working_dir,
                target_uuid=request.target_uuid,
            )
        except Exception as anchor_err:
            logger.error(
                f"Rewind DB anchor resolution failed for session {session_id}: {anchor_err}",
                exc_info=True,
            )

        # (a) Truncate the Postgres session store — the authoritative chat the
        # UI reads. Skipping it is the exact bug where rewind "succeeds" but
        # the message stays in chat because it still lives in the store and
        # reappears on reload.
        store_removed = 0
        store_truncated = False
        try:
            store_removed = await truncate_session_store_to_checkpoint(
                sdk_session_id=sdk_session_id,
                target_uuid=request.target_uuid,
                include_response=request.include_response,
                target_role=request.target_role,
            )
            store_truncated = True
        except RewindTargetNotFound as store_nf:
            logger.warning(
                f"Rewind store truncation: target not found for "
                f"session={session_id} target={request.target_uuid}: {store_nf}"
            )
            errors.append(f"Store rewind failed: {store_nf}")
        except Exception as store_err:
            logger.error(
                f"Rewind store truncation failed for session {session_id}: {store_err}",
                exc_info=True,
            )
            errors.append(f"Store rewind failed: {store_err}")

        # (b) Truncate the on-disk JSONL. Best-effort: the tmpfs transcript may
        # be gone on a fresh server and the store is authoritative.
        result = jsonl_rewind_service.truncate_to_checkpoint(
            sdk_session_id=sdk_session_id,
            target_uuid=request.target_uuid,
            working_dir=working_dir,
            include_response=request.include_response,
            target_role=request.target_role
        )
        jsonl_ok = result.success
        if jsonl_ok:
            messages_removed = result.messages_removed or 0

        # Mirror the truncation into session_messages whenever ANY transcript
        # layer was actually truncated. Without this, rewind leaves orphan rows
        # the UI renders on reload (the original "rewind doesn't stick"
        # symptom). Gating it on jsonl_ok alone left the legacy rows intact
        # whenever only the store could be truncated — e.g. with the (tmpfs,
        # often absent) JSONL gone — and they
        # then resurfaced through the empty-store legacy fallback after a full
        # rewind. Best-effort: a DB failure here does not invalidate a
        # successful transcript truncation.
        # Memo for the SDK-transcript emptiness probe: the no-anchor purge
        # below may already pay for it; the pointer-clear check reuses the
        # answer instead of re-probing.
        transcript_probe: Optional[bool] = None
        if jsonl_ok or store_truncated:
            db_removed = 0
            if anchor_id is not None:
                try:
                    db_removed = await asyncio.to_thread(
                        apply_rewind_delete,
                        session_id=session_id,
                        anchor_id=anchor_id,
                        include_response=request.include_response,
                    )
                except Exception as db_err:
                    logger.error(
                        f"Rewind DB sync failed for session {session_id}: {db_err}",
                        exc_info=True,
                    )
                    errors.append(f"DB sync failed: {db_err}")
            else:
                # No legacy anchor row could be resolved. Usually benign
                # (post-cutover session_messages holds user rows only, so an
                # assistant-target anchor can miss legitimately) — EXCEPT after
                # a full rewind that emptied the store: the read path's
                # empty-store legacy fallback would then resurrect every
                # leftover session_messages row on reload. Nothing is left to
                # anchor on, so wipe them all.
                purged_legacy = False
                if store_truncated:
                    try:
                        transcript_probe = await main_transcript_is_empty(
                            sdk_session_id
                        )
                        if transcript_probe:
                            db_removed = await asyncio.to_thread(
                                delete_all_session_rows, session_id
                            )
                            purged_legacy = True
                    except Exception as purge_err:
                        logger.error(
                            f"Rewind legacy purge failed for session {session_id}: {purge_err}",
                            exc_info=True,
                        )
                if not purged_legacy:
                    logger.warning(
                        f"Rewind DB sync skipped: no anchor row resolved for "
                        f"session={session_id} target={request.target_uuid}. "
                        f"Legacy session_messages rows were left untouched; they "
                        f"only surface through the empty-store legacy fallback."
                    )
            logger.info(
                f"Rewind DB sync: session={session_id}, target_uuid={request.target_uuid}, "
                f"db_rows_removed={db_removed}, jsonl_messages_removed={messages_removed}"
            )
        if jsonl_ok:
            # Update turn_count in DB so sidebar correctly hides empty sessions.
            if result.remaining_user_turns is not None:
                current_turns = session.get("turn_count", 0)
                turn_diff = result.remaining_user_turns - current_turns
                if turn_diff != 0:
                    # Isolated single write: `current_turns` comes from the
                    # session dict already loaded above, not a fresh read, so
                    # there is no read-decide-write pair to keep together here.
                    await run_sync(
                        database.update_session, session_id, turn_increment=turn_diff
                    )

        # Overall chat-rewind success: the store is the chat.
        chat_rewound = store_truncated

        if chat_rewound:
            # Report a non-zero count even when only the store was truncated.
            if not messages_removed and store_removed:
                messages_removed = store_removed
            # Invalidate the parse caches keyed by sdk_session_id. The
            # store-backed _store_parse_cache is (count, max_id)-versioned, so
            # a remote install's cache self-heals on read — but dropping the
            # local entry here makes the truncation visible immediately without
            # spending a version probe. Also drops the mtime-validated JSONL
            # cache so filesystem-mode reads re-parse the truncated file.
            invalidate_parse_cache(sdk_session_id)
            # V2: no in-memory live overlay to trim — reads serve the persisted
            # transcript, so the truncation is visible on the very next load.

            # A rewind that emptied the transcript leaves the CLI nothing to
            # resume — the stale sdk_session_id makes the very next turn fail
            # with "No conversation found". Clear the pointer (empty string →
            # NULL) so the next message starts a fresh SDK session.
            transcript_empty = jsonl_ok and result.remaining_user_turns == 0
            if not transcript_empty and store_truncated:
                if transcript_probe is not None:
                    transcript_empty = transcript_probe
                else:
                    try:
                        transcript_empty = await main_transcript_is_empty(
                            sdk_session_id
                        )
                    except Exception:
                        transcript_empty = False
            if transcript_empty:
                try:
                    await asyncio.to_thread(
                        database.update_session, session_id, sdk_session_id=""
                    )
                except Exception as clear_err:
                    logger.error(
                        f"Rewind sdk_session_id clear failed for session "
                        f"{session_id}: {clear_err}",
                        exc_info=True,
                    )
                    errors.append(
                        "Could not clear the SDK resume pointer; the next "
                        "message may fail to start"
                    )

    # Determine overall success
    # Chat rewind is the primary operation - if it succeeds, the operation is successful
    # File rewind failure is a partial success, not a total failure
    chat_success = not request.restore_chat or chat_rewound

    # Success if chat rewind worked (or wasn't requested)
    success = chat_success

    # Partial success: chat worked but files didn't (when files were requested)
    partial_success = chat_success and request.restore_files and not files_rewound

    # Warning message for partial success
    warning = None
    if partial_success:
        warning = "Chat rewound successfully. File restore unavailable - no checkpoint exists for this point."

    # Honesty guard for fluid sessions running the DELEGATED (claude CLI) engine.
    # The CLI keeps its OWN conversation history and fluid cannot truncate it
    # (``DelegatedLoop.caps`` declares rewind=False; ``--resume`` is its only
    # history control), so without the re-seed above the model could still
    # recall the removed turns.
    #
    # The re-seed removes that divergence at the source: the session's
    # ``--resume`` handle points at a freshly materialized transcript that ends
    # at the checkpoint (or is dropped entirely when the rewind emptied the
    # conversation), and a native-engine session never had CLI history to begin
    # with. The warning is therefore reserved for the one case where the context
    # really can still diverge: the re-seed itself failed.
    if fluid_session and chat_rewound and reseed_result is not None:
        if not reseed_result.context_is_authoritative:
            divergence = (
                "Transcript rewound, but the delegated claude-CLI history could "
                "not be re-seeded, so the model may still recall the removed "
                "turns."
            )
            warning = f"{warning} {divergence}" if warning else divergence

    # Build result message
    if success and not partial_success:
        parts = []
        if chat_rewound:
            parts.append(f"removed {messages_removed} messages")
        if files_rewound:
            parts.append("restored files")
        message = f"Successfully rewound: {', '.join(parts)}" if parts else "Rewind completed"
    elif partial_success:
        message = f"Chat rewound (removed {messages_removed} messages), but file restore was unavailable"
    else:
        message = "Rewind failed"

    # v2 event log: fold the successful chat rewind into the append-only log
    # (unfenced — a rewind happens between turns, with no owning run). A
    # resolved-but-tail target (rewound_from_seq is None) appends nothing.
    if v2_session and chat_rewound and rewound_from_seq is not None:
        try:
            from app.contract import events as contract_events
            from app.core.turn_recorder import _build_event
            from app.db import session_events as session_events_db
            await asyncio.to_thread(
                session_events_db.append_events,
                session_id,
                [_build_event(
                    contract_events.TranscriptRewound, session_id,
                    from_seq=rewound_from_seq,
                )],
            )
        except Exception as rewound_err:
            logger.error(
                f"Rewind transcript.rewound append failed for session "
                f"{session_id} (from_seq={rewound_from_seq}): {rewound_err}",
                exc_info=True,
            )
            errors.append(
                "Event-log sync failed: v2 replicas may still show the "
                "rewound messages"
            )

    # Broadcast rewind event to all connected devices for this session
    if chat_rewound and messages_removed > 0:
        asyncio.create_task(
            sync_engine.broadcast_session_rewound(
                session_id=session_id,
                target_uuid=request.target_uuid,
                messages_removed=messages_removed
            )
        )

    return RewindResponseV2(
        success=success,
        message=message,
        chat_rewound=chat_rewound,
        files_rewound=files_rewound,
        messages_removed=messages_removed,
        error="; ".join(errors) if errors else None,
        partial_success=partial_success,
        warning=warning
    )


# Legacy endpoints for backwards compatibility
@router.get("/rewind/checkpoints/{session_id}/legacy", response_model=RewindCheckpointsResponse)
async def get_rewind_checkpoints_legacy(session_id: str, http_request: Request = None):
    """
    Legacy endpoint - redirects to V2.

    Maintained for backwards compatibility with older frontends.
    """
    v2_response = await get_rewind_checkpoints(session_id, http_request)

    # Convert V2 response to legacy format
    legacy_checkpoints = [
        RewindCheckpoint(
            index=cp.index,
            message=cp.message_preview,
            full_message=cp.full_message,
            timestamp=cp.timestamp,
            is_current=(cp.index == len(v2_response.checkpoints) - 1)
        )
        for cp in v2_response.checkpoints
    ]

    return RewindCheckpointsResponse(
        success=v2_response.success,
        session_id=session_id,
        checkpoints=legacy_checkpoints,
        error=v2_response.error
    )


@router.post("/rewind/execute/{session_id}/legacy", response_model=RewindExecuteResponse)
async def execute_rewind_legacy(session_id: str, request: RewindRequest, http_request: Request = None):
    """
    Legacy endpoint - converts old format to V2.

    Maintained for backwards compatibility with older frontends.
    """
    # Get checkpoints to find UUID for the index. The session read, the project
    # resolution and the JSONL transcript parse are contiguous and synchronous —
    # ONE hop covers all three.
    def _load_checkpoints():
        session = database.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if http_request is not None:
            check_session_access(http_request, session)

        working_dir = get_working_dir_for_project(session.get("project_id"))
        return _get_checkpoints(session.get("sdk_session_id"), working_dir)

    checkpoints = await run_sync(_load_checkpoints)

    if request.checkpoint_index >= len(checkpoints):
        return RewindExecuteResponse(
            success=False,
            message="Invalid checkpoint index",
            error=f"Index {request.checkpoint_index} out of range (max {len(checkpoints) - 1})"
        )

    target_uuid = checkpoints[request.checkpoint_index].uuid

    # Map legacy restore_option to V2 flags
    restore_chat = request.restore_option in [1, 2]
    restore_code = request.restore_option in [1, 3]

    if request.restore_option == 4:
        return RewindExecuteResponse(
            success=True,
            message="Rewind cancelled",
            checkpoint_index=request.checkpoint_index,
            restore_option=4
        )

    # Execute V2 rewind
    v2_request = RewindRequestV2(
        target_uuid=target_uuid,
        restore_chat=restore_chat,
        restore_files=restore_code,
        include_response=True
    )

    v2_response = await execute_rewind(session_id, v2_request, http_request)

    return RewindExecuteResponse(
        success=v2_response.success,
        message=v2_response.message,
        checkpoint_index=request.checkpoint_index,
        restore_option=request.restore_option,
        error=v2_response.error
    )

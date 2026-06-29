"""
REST API for slash commands and rewind operations.

Provides endpoints for:
- Listing available commands (for autocomplete)
- Getting command details
- Executing non-interactive custom commands
- Rewind operations (list checkpoints, execute rewind)

V2 Rewind: Uses direct JSONL manipulation instead of PTY-based CLI bridge.
This is bulletproof - no terminal parsing, no race conditions.
"""

import logging
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import database
from app.core.config import settings
from app.core.project_utils import get_project_working_dir, get_worktree_working_dir
from app.api.common import resolve_project_path
from app.core.slash_commands import (
    get_command_by_name, get_all_commands,
    is_interactive_command, is_rest_api_command, get_rest_api_command_info,
    parse_command_input
)
# New V2 rewind services - direct JSONL manipulation
from app.core.jsonl_rewind import jsonl_rewind_service
from app.core.pg_session_store import (
    RewindTargetNotFound,
    truncate_session_store_to_checkpoint,
)
from app.core.rewind_db_sync import resolve_rewind_anchor, apply_rewind_delete
from app.core.sync_engine import sync_engine
from app.core.live_session_store import live_session_store
from app.core.models import (
    RewindRequest, RewindCheckpoint, RewindCheckpointsResponse,
    RewindExecuteResponse
)
# SDK client for file rewind operations
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/commands", tags=["Commands"])


class CommandInfo(BaseModel):
    """Command information for API responses"""
    name: str
    display: str
    description: str
    argument_hint: Optional[str] = None
    type: str  # "custom" or "interactive"
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


class ExecuteCommandRequest(BaseModel):
    """Request to execute a custom command"""
    command: str  # Command with arguments, e.g., "/fix-issue 123 high"
    session_id: str


class ExecuteCommandResponse(BaseModel):
    """Response from command execution"""
    success: bool
    message: str
    expanded_prompt: Optional[str] = None
    is_interactive: bool = False


def get_working_dir_for_project(project_id: Optional[str], worktree_id: Optional[str] = None) -> str:
    """Get the working directory for a project, or its worktree if specified.

    .. deprecated:: Use :func:`app.api.common.resolve_project_path` instead.
    """
    result = resolve_project_path(
        project_id,
        check_access=False,
        worktree_id=worktree_id,
        fallback_to_workspace=True,
    )
    return str(result)


@router.get("/", response_model=CommandListResponse)
async def list_commands(
    project_id: Optional[str] = Query(None, description="Project ID to get commands for"),
    worktree_id: Optional[str] = Query(None, description="Worktree ID (overrides project path when in worktree mode)")
):
    """
    List all available slash commands.

    Returns both custom commands from the project's .claude/commands/
    directory and built-in interactive commands like /rewind.
    """
    working_dir = get_working_dir_for_project(project_id, worktree_id)
    commands = get_all_commands(working_dir)

    return CommandListResponse(
        commands=[CommandInfo(**cmd) for cmd in commands],
        count=len(commands)
    )


@router.get("/{command_name}", response_model=CommandDetailResponse)
async def get_command(
    command_name: str,
    project_id: Optional[str] = Query(None, description="Project ID"),
    worktree_id: Optional[str] = Query(None, description="Worktree ID (overrides project path when in worktree mode)")
):
    """
    Get detailed information about a specific command.

    The command_name should not include the leading slash.
    """
    # Check if it's an interactive command
    if is_interactive_command(command_name):
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
    working_dir = get_working_dir_for_project(project_id, worktree_id)
    cmd = get_command_by_name(working_dir, command_name)

    if not cmd:
        raise HTTPException(status_code=404, detail=f"Command not found: {command_name}")

    return CommandDetailResponse(
        name=cmd.name,
        display=cmd.get_display_name(),
        description=cmd.description,
        content=cmd.content,
        argument_hint=cmd.argument_hint,
        type="custom",
        source=cmd.source,
        namespace=cmd.namespace,
        allowed_tools=cmd.allowed_tools,
        model=cmd.model,
        is_interactive=False
    )


class ExecuteCommandResponseV2(BaseModel):
    """Enhanced response from command execution with REST API support"""
    success: bool
    message: str
    expanded_prompt: Optional[str] = None
    is_interactive: bool = False
    is_rest_api: bool = False
    api_endpoint: Optional[str] = None


@router.post("/execute", response_model=ExecuteCommandResponse)
async def execute_command(request: ExecuteCommandRequest):
    """
    Execute a custom slash command.

    For custom commands, this expands the prompt with arguments
    and returns the expanded prompt for the client to send as a query.

    For interactive commands (like /resume), this returns is_interactive=True
    and the client should use the CLI WebSocket endpoint instead.

    For REST API commands (like /rewind), this returns is_rest_api=True
    and the client should use the dedicated REST API endpoints.
    """
    # Parse command input
    command_name, arguments = parse_command_input(request.command)

    if not command_name:
        raise HTTPException(status_code=400, detail="Invalid command format")

    # Check if it's an interactive command (like /resume)
    if is_interactive_command(command_name):
        return ExecuteCommandResponse(
            success=True,
            message=f"Command /{command_name} requires interactive terminal",
            is_interactive=True
        )

    # Check if it's a REST API command (like /rewind)
    if is_rest_api_command(command_name):
        info = get_rest_api_command_info(command_name)
        api_endpoint = info.get("api_endpoint", "").replace("{session_id}", request.session_id)
        return ExecuteCommandResponse(
            success=True,
            message=f"Command /{command_name} uses REST API - use {api_endpoint}",
            is_interactive=False
        )

    # Get session to find project
    session = database.get_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    project_id = session.get("project_id")
    working_dir = get_working_dir_for_project(project_id)

    # Get command
    cmd = get_command_by_name(working_dir, command_name)
    if not cmd:
        raise HTTPException(status_code=404, detail=f"Command not found: {command_name}")

    # Check if arguments are required but not provided
    if cmd.argument_hint and not arguments:
        return ExecuteCommandResponse(
            success=False,
            message=f"Command requires arguments: {cmd.argument_hint}",
            expanded_prompt=None
        )

    # Expand the prompt
    expanded_prompt = cmd.expand_prompt(arguments)

    return ExecuteCommandResponse(
        success=True,
        message="Command expanded successfully",
        expanded_prompt=expanded_prompt,
        is_interactive=False
    )


@router.post("/sync-after-rewind")
async def sync_after_rewind(
    session_id: str = Query(..., description="Session ID"),
    checkpoint_message: str = Query(..., description="The user message text at the checkpoint"),
    restore_option: int = Query(..., description="Restore option (1-4)")
):
    """
    Sync our chat database after a rewind operation completes.

    This is called by the frontend after the user completes a /rewind
    in the CLI terminal. It deletes messages after the selected checkpoint
    to keep our chat in sync with Claude's context.

    Options:
    1 = Restore code and conversation - delete messages after checkpoint
    2 = Restore conversation - delete messages after checkpoint
    3 = Restore code - don't delete messages (code-only revert)
    4 = Never mind - don't delete anything
    """
    if restore_option not in [1, 2, 3, 4]:
        raise HTTPException(status_code=400, detail="Invalid restore option")

    # Option 3 and 4 don't require chat sync
    if restore_option in [3, 4]:
        return {
            "success": True,
            "message": "No chat sync needed for this option",
            "deleted_count": 0
        }

    # Get session
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get all messages for this session
    messages = database.get_session_messages(session_id)

    # Find the checkpoint message (user message matching the checkpoint text)
    checkpoint_index = -1
    for i, msg in enumerate(messages):
        if msg["role"] == "user" and checkpoint_message in msg["content"]:
            checkpoint_index = i
            break

    if checkpoint_index == -1:
        # Try partial match
        for i, msg in enumerate(messages):
            if msg["role"] == "user" and (
                msg["content"].startswith(checkpoint_message[:50]) or
                checkpoint_message.startswith(msg["content"][:50])
            ):
                checkpoint_index = i
                break

    if checkpoint_index == -1:
        logger.warning(f"Could not find checkpoint message: {checkpoint_message[:50]}...")
        return {
            "success": False,
            "message": "Could not find checkpoint message in chat history",
            "deleted_count": 0
        }

    # Delete all messages after the checkpoint
    messages_to_delete = messages[checkpoint_index + 1:]
    deleted_count = 0

    for msg in messages_to_delete:
        try:
            database.delete_session_message(session_id, msg["id"])
            deleted_count += 1
        except Exception as e:
            logger.error(f"Failed to delete message {msg['id']}: {e}")

    logger.debug(f"Synced chat after rewind: deleted {deleted_count} messages after checkpoint")

    return {
        "success": True,
        "message": f"Deleted {deleted_count} messages after checkpoint",
        "deleted_count": deleted_count,
        "checkpoint_index": checkpoint_index
    }


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
    target_uuid: str  # UUID of the message to rewind to
    restore_chat: bool = True  # Truncate JSONL (rewind conversation)
    restore_files: bool = False  # Restore files to checkpoint state (requires file checkpointing enabled)
    include_response: bool = True  # Keep Claude's response to target message
    target_role: str = "user"  # Role of target message ("user" or "assistant")


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


async def rewind_files_via_sdk(
    sdk_session_id: str,
    target_uuid: str,
    working_dir: str
) -> tuple[bool, Optional[str]]:
    """
    Rewind files to a checkpoint using the Claude SDK.

    This creates a temporary SDK client, resumes the session with an empty prompt,
    and calls rewind_files() within the response loop per the SDK's documented pattern.

    IMPORTANT: This requires that the session was originally created with
    enable_file_checkpointing=True in ClaudeAgentOptions. If file checkpointing
    was not enabled when the session started, this operation will fail.

    Args:
        sdk_session_id: The SDK session ID to resume
        target_uuid: UUID of the user message to rewind files to
        working_dir: Working directory for the project

    Returns:
        Tuple of (success, error_message)
    """
    try:
        # Create options for file rewind - must include enable_file_checkpointing
        # even when resuming to ensure SDK knows about the checkpointing state
        options = ClaudeAgentOptions(
            resume=sdk_session_id,
            cwd=working_dir,
            enable_file_checkpointing=True,  # Required for rewind_files() operation
        )

        logger.debug(f"Rewinding files for session {sdk_session_id} to UUID {target_uuid}")

        # Per SDK docs: resume the session with an empty prompt, then call
        # rewind_files() inside the receive_response() loop. The CLI subprocess
        # needs an active query/response cycle for control requests to work.
        async with ClaudeSDKClient(options) as client:
            await client.query("")  # Empty prompt opens the connection
            async for message in client.receive_response():
                await client.rewind_files(target_uuid)
                break

        logger.debug(f"Successfully rewound files for session {sdk_session_id} to {target_uuid}")
        return True, None

    except Exception as e:
        error_msg = f"File rewind failed: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


@router.get("/rewind/checkpoints/{session_id}")
async def get_rewind_checkpoints(session_id: str):
    """
    Get available checkpoints for a session that can be rewound to.

    Uses JSONL rewind service to parse checkpoints directly from JSONL file.
    """
    # Get session info
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

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
    checkpoints = jsonl_rewind_service.get_checkpoints(sdk_session_id, working_dir)

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


@router.post("/rewind/execute/{session_id}")
async def execute_rewind(session_id: str, request: RewindRequestV2):
    """
    Execute a rewind operation to restore conversation and optionally files.

    Direct JSONL truncation - bulletproof, no PTY/terminal needed.

    How it works:
    1. Truncates the JSONL file at the target message UUID
    2. Optionally rewinds files via SDK (if enable_file_checkpointing was enabled)
    3. Syncs our local database
    4. Next SDK resume will use truncated context

    Options:
    - restore_chat: Truncate JSONL (rewind conversation context)
    - restore_files: Restore files to checkpoint state (requires file checkpointing)
    - include_response: Keep Claude's response to target message
    """
    # Get session info
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

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

    # Get working directory for project
    working_dir = get_working_dir_for_project(session.get("project_id"))

    logger.info(
        f"Rewind requested: session={session_id}, sdk_session={sdk_session_id}, "
        f"target_uuid={request.target_uuid}, target_role={request.target_role}, "
        f"restore_chat={request.restore_chat}, restore_files={request.restore_files}, "
        f"include_response={request.include_response}"
    )

    # Track what was rewound
    chat_rewound = False
    files_rewound = False
    messages_removed = 0
    errors = []

    # IMPORTANT: File rewind must happen BEFORE chat rewind.
    # Chat rewind truncates the JSONL file, which can remove the target message UUID.
    # The SDK needs that UUID present in the JSONL to find the file checkpoint.
    if request.restore_files:
        file_success, file_error = await rewind_files_via_sdk(
            sdk_session_id=sdk_session_id,
            target_uuid=request.target_uuid,
            working_dir=working_dir
        )
        if file_success:
            files_rewound = True
        else:
            errors.append(file_error or "File rewind failed")

    # Execute chat rewind. There are up to three storage layers to keep in sync:
    #   (a) the Postgres session store (``session_store_entries``) — the source
    #       of truth the UI reads in ``postgres_only`` mode,
    #   (b) the on-disk JSONL transcript the SDK resumes from, and
    #   (c) the legacy ``session_messages`` rows.
    # Which one is authoritative depends on ``session_store_mode``.
    if request.restore_chat:
        store_mode = settings.session_store_mode
        postgres_only = store_mode == "postgres_only"
        store_truncation_enabled = store_mode in ("mirror", "postgres_only")

        # (a) Truncate the Postgres session store FIRST. In postgres_only this
        # is the authoritative chat the UI reads — skipping it is the exact bug
        # where rewind "succeeds" but the message stays in chat because it still
        # lives in the store and reappears on reload.
        store_removed = 0
        store_truncated = False
        if store_truncation_enabled:
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
                if postgres_only:
                    errors.append(f"Store rewind failed: {store_nf}")
            except Exception as store_err:
                logger.error(
                    f"Rewind store truncation failed for session {session_id}: {store_err}",
                    exc_info=True,
                )
                if postgres_only:
                    errors.append(f"Store rewind failed: {store_err}")

        # (c) Resolve the DB anchor BEFORE truncating the JSONL. The fallback
        # content-matcher (B) needs the JSONL intact to look up the target
        # uuid's text when user rows lack a persisted jsonl_uuid. This also
        # runs the (A) backfill so subsequent rewinds hit the fast path.
        anchor_id: Optional[int] = None
        try:
            anchor_id = resolve_rewind_anchor(
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

        # (b) Truncate the on-disk JSONL. Primary signal in disabled/mirror;
        # best-effort in postgres_only, where the tmpfs transcript may be gone
        # on a fresh server and the store is authoritative.
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
            # Mirror the JSONL truncation into session_messages. Without this,
            # rewind leaves orphan rows the UI renders on reload (the original
            # "rewind doesn't stick" symptom). Best-effort: a DB failure here
            # does not invalidate the successful JSONL truncation.
            db_removed = 0
            if anchor_id is not None:
                try:
                    db_removed = apply_rewind_delete(
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
            elif not postgres_only:
                # In postgres_only, session_messages only holds user rows and is
                # not what the UI reads, so a missing anchor there is expected.
                logger.warning(
                    f"Rewind DB sync skipped: no anchor row resolved for "
                    f"session={session_id} target={request.target_uuid}. "
                    f"UI may show stale messages until next reload with backfill."
                )
                errors.append("DB sync skipped: could not resolve anchor row")
            logger.info(
                f"Rewind DB sync: session={session_id}, target_uuid={request.target_uuid}, "
                f"db_rows_removed={db_removed}, jsonl_messages_removed={messages_removed}"
            )
            # Update turn_count in DB so sidebar correctly hides empty sessions.
            if result.remaining_user_turns is not None:
                current_turns = session.get("turn_count", 0)
                turn_diff = result.remaining_user_turns - current_turns
                if turn_diff != 0:
                    database.update_session(session_id, turn_increment=turn_diff)
        elif not postgres_only:
            errors.append(f"Chat rewind failed: {result.error}")

        # Overall chat-rewind success: in postgres_only the store is the chat;
        # elsewhere the JSONL is. (Either succeeding counts everywhere else so a
        # mirror-mode store hit isn't lost if the JSONL is unexpectedly absent.)
        if postgres_only:
            chat_rewound = store_truncated
        else:
            chat_rewound = jsonl_ok or store_truncated

        if chat_rewound:
            # Report a non-zero count even when only the store was truncated.
            if not messages_removed and store_removed:
                messages_removed = store_removed
            # Clear stale messages from live_session_store so the next
            # load_session reads truncated data instead of serving pre-rewind
            # data from memory.
            await live_session_store.cleanup_ended_session(session_id)

    # Determine overall success
    # Chat rewind is the primary operation - if it succeeds, the operation is successful
    # File rewind failure is a partial success, not a total failure
    chat_success = not request.restore_chat or chat_rewound
    file_success = not request.restore_files or files_rewound

    # Success if chat rewind worked (or wasn't requested)
    success = chat_success

    # Partial success: chat worked but files didn't (when files were requested)
    partial_success = chat_success and request.restore_files and not files_rewound

    # Warning message for partial success
    warning = None
    if partial_success:
        warning = "Chat rewound successfully. File restore unavailable - no checkpoint exists for this point."

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

    # Broadcast rewind event to all connected devices for this session
    if chat_rewound and messages_removed > 0:
        import asyncio
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
async def get_rewind_checkpoints_legacy(session_id: str):
    """
    Legacy endpoint - redirects to V2.

    Maintained for backwards compatibility with older frontends.
    """
    v2_response = await get_rewind_checkpoints(session_id)

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
async def execute_rewind_legacy(session_id: str, request: RewindRequest):
    """
    Legacy endpoint - converts old format to V2.

    Maintained for backwards compatibility with older frontends.
    """
    # Get checkpoints to find UUID for the index
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    sdk_session_id = session.get("sdk_session_id")
    working_dir = get_working_dir_for_project(session.get("project_id"))

    checkpoints = jsonl_rewind_service.get_checkpoints(sdk_session_id, working_dir)

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
        restore_code=restore_code,
        include_response=True
    )

    v2_response = await execute_rewind(session_id, v2_request)

    return RewindExecuteResponse(
        success=v2_response.success,
        message=v2_response.message,
        checkpoint_index=request.checkpoint_index,
        restore_option=request.restore_option,
        error=v2_response.error
    )


@router.get("/rewind/status")
async def get_rewind_status():
    """
    Get current rewind status.

    V2: No longer uses pending rewind configuration.
    Returns info about rewind capability.
    """
    return {
        "version": "v2",
        "method": "direct_jsonl",
        "description": "Rewind via direct JSONL truncation - no PTY/CLI bridge needed",
        "has_pending": False,
        "pending_rewind": None
    }


@router.post("/rewind/clear")
async def clear_pending_rewind():
    """
    Clear any pending rewind configuration.

    V2: No-op since we no longer use pending rewind config.
    """
    return {
        "success": True,
        "message": "V2 rewind doesn't use pending configuration"
    }

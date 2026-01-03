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
from app.core.slash_commands import (
    get_command_by_name, get_all_commands,
    is_interactive_command, is_rest_api_command, get_rest_api_command_info,
    parse_command_input
)
# New V2 rewind services - direct JSONL manipulation
from app.core.jsonl_rewind import jsonl_rewind_service
from app.core.checkpoint_manager import checkpoint_manager
from app.core.sync_engine import sync_engine
from app.core.models import (
    RewindRequest, RewindCheckpoint, RewindCheckpointsResponse,
    RewindExecuteResponse
)

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


def get_working_dir_for_project(project_id: Optional[str]) -> str:
    """Get the working directory for a project"""
    if project_id:
        project = database.get_project(project_id)
        if project:
            return str(settings.workspace_dir / project["path"])
    return str(settings.workspace_dir)


@router.get("/", response_model=CommandListResponse)
async def list_commands(
    project_id: Optional[str] = Query(None, description="Project ID to get commands for")
):
    """
    List all available slash commands.

    Returns both custom commands from the project's .claude/commands/
    directory and built-in interactive commands like /rewind.
    """
    working_dir = get_working_dir_for_project(project_id)
    commands = get_all_commands(working_dir)

    return CommandListResponse(
        commands=[CommandInfo(**cmd) for cmd in commands],
        count=len(commands)
    )


@router.get("/{command_name}", response_model=CommandDetailResponse)
async def get_command(
    command_name: str,
    project_id: Optional[str] = Query(None, description="Project ID")
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
    working_dir = get_working_dir_for_project(project_id)
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

    logger.info(f"Synced chat after rewind: deleted {deleted_count} messages after checkpoint")

    return {
        "success": True,
        "message": f"Deleted {deleted_count} messages after checkpoint",
        "deleted_count": deleted_count,
        "checkpoint_index": checkpoint_index
    }


# =============================================================================
# Rewind API V2 - Direct JSONL manipulation (bulletproof)
# =============================================================================

class RewindRequestV2(BaseModel):
    """V2 Rewind request using message UUID instead of index"""
    target_uuid: str  # UUID of the message to rewind to
    restore_chat: bool = True  # Truncate JSONL (rewind conversation)
    restore_code: bool = False  # Restore git snapshot (rewind code)
    include_response: bool = True  # Keep Claude's response to target message


class RewindResponseV2(BaseModel):
    """V2 Rewind response with detailed results"""
    success: bool
    message: str
    chat_rewound: bool = False
    code_rewound: bool = False
    messages_removed: int = 0
    error: Optional[str] = None


class CheckpointV2(BaseModel):
    """V2 Checkpoint with UUID and git info"""
    uuid: str
    index: int
    message_preview: str
    full_message: str
    timestamp: Optional[str] = None
    git_available: bool = False
    git_ref: Optional[str] = None
    has_changes_after: bool = False  # Whether there are git snapshots after this checkpoint


class CheckpointsResponseV2(BaseModel):
    """V2 Response containing checkpoints"""
    success: bool
    session_id: str
    sdk_session_id: Optional[str] = None
    checkpoints: List[CheckpointV2] = []
    error: Optional[str] = None


@router.get("/rewind/checkpoints/{session_id}")
async def get_rewind_checkpoints(session_id: str):
    """
    Get available checkpoints for a session that can be rewound to.

    V2: Uses CheckpointManager which combines JSONL checkpoints with
    persisted git snapshot information from the database.
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

    # Get checkpoints via CheckpointManager (combines JSONL + database git_refs)
    checkpoints = checkpoint_manager.get_checkpoints(session_id, include_git=True)

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
                    git_available=False,
                    git_ref=None
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
            uuid=cp['message_uuid'],
            index=cp['message_index'],
            message_preview=cp['message_preview'],
            full_message=cp.get('full_message', cp['message_preview']),
            timestamp=cp.get('timestamp'),
            git_available=cp.get('git_available', False),
            git_ref=cp.get('git_ref'),
            has_changes_after=cp.get('has_changes_after', False)
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
    Execute a rewind operation to restore conversation and/or code.

    V2: Direct JSONL truncation - bulletproof, no PTY/terminal needed.

    How it works:
    1. Truncates the JSONL file at the target message UUID
    2. Optionally restores git snapshot for code changes
    3. Syncs our local database
    4. Next SDK resume will use truncated context

    Options:
    - restore_chat: Truncate JSONL (rewind conversation context)
    - restore_code: Restore git snapshot (rewind file changes)
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

    # Execute rewind using checkpoint manager
    result = checkpoint_manager.rewind(
        session_id=session_id,
        target_uuid=request.target_uuid,
        restore_chat=request.restore_chat,
        restore_code=request.restore_code,
        include_response=request.include_response
    )

    # Broadcast rewind event to all connected devices for this session
    if result.success:
        import asyncio
        asyncio.create_task(
            sync_engine.broadcast_session_rewound(
                session_id=session_id,
                target_uuid=request.target_uuid,
                messages_removed=result.messages_removed or 0
            )
        )

    return RewindResponseV2(
        success=result.success,
        message=result.message,
        chat_rewound=result.chat_rewound,
        code_rewound=result.code_rewound,
        messages_removed=result.messages_removed,
        error=result.error
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

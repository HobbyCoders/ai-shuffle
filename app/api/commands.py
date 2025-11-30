"""
REST API for slash commands and rewind operations.

Provides endpoints for:
- Listing available commands (for autocomplete)
- Getting command details
- Executing non-interactive custom commands
- Rewind operations (list checkpoints, execute rewind)
"""

import logging
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.db import database
from app.core.config import settings
from app.core.slash_commands import (
    discover_commands, get_command_by_name, get_all_commands,
    is_interactive_command, is_rest_api_command, get_rest_api_command_info,
    parse_command_input, SlashCommand
)
from app.core.rewind_manager import rewind_manager
from app.core.models import (
    RewindRequest, RewindCheckpoint, RewindCheckpointsResponse,
    RewindExecuteResponse, RewindStatus
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
# Rewind API - Settings-based (non-interactive) rewind operations
# =============================================================================

@router.get("/rewind/checkpoints/{session_id}", response_model=RewindCheckpointsResponse)
async def get_rewind_checkpoints(session_id: str):
    """
    Get available checkpoints for a session that can be rewound to.

    This reads the session's conversation history and returns checkpoints
    (user messages) that Claude can rewind to.
    """
    # Get session info
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    sdk_session_id = session.get("sdk_session_id")
    if not sdk_session_id:
        return RewindCheckpointsResponse(
            success=False,
            session_id=session_id,
            checkpoints=[],
            error="Session has no SDK session ID - cannot get checkpoints"
        )

    # Get working directory
    working_dir = str(settings.workspace_dir)
    project_id = session.get("project_id")
    if project_id:
        project = database.get_project(project_id)
        if project:
            working_dir = str(settings.workspace_dir / project["path"])

    # Get checkpoints from rewind manager
    result = rewind_manager.get_session_checkpoints(sdk_session_id, working_dir)

    if not result["success"]:
        # Fallback: get checkpoints from our local database
        messages = database.get_session_messages(session_id)
        checkpoints = []

        for i, msg in enumerate(messages):
            if msg["role"] == "user":
                content = msg["content"]
                display_content = content[:100] + '...' if len(content) > 100 else content

                checkpoints.append(RewindCheckpoint(
                    index=len(checkpoints),
                    message=display_content,
                    full_message=content,
                    timestamp=str(msg.get("created_at", "")),
                    is_current=(i == len(messages) - 1 or i == len(messages) - 2)
                ))

        return RewindCheckpointsResponse(
            success=True,
            session_id=session_id,
            checkpoints=checkpoints
        )

    # Convert to response model
    checkpoints = [
        RewindCheckpoint(
            index=cp["index"],
            message=cp["message"],
            full_message=cp.get("full_message"),
            timestamp=cp.get("timestamp"),
            is_current=cp.get("is_current", False)
        )
        for cp in result["checkpoints"]
    ]

    return RewindCheckpointsResponse(
        success=True,
        session_id=session_id,
        checkpoints=checkpoints
    )


@router.post("/rewind/execute/{session_id}", response_model=RewindExecuteResponse)
async def execute_rewind(session_id: str, request: RewindRequest):
    """
    Execute a rewind operation to restore conversation and/or code.

    This is a non-interactive rewind that uses the settings-based approach
    similar to Claude Code authentication.

    Restore options:
    - 1: Restore code and conversation
    - 2: Restore conversation only
    - 3: Restore code only
    - 4: Cancel (never mind)
    """
    # Get session info
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    sdk_session_id = session.get("sdk_session_id")
    if not sdk_session_id:
        return RewindExecuteResponse(
            success=False,
            message="Cannot execute rewind",
            error="Session has no SDK session ID"
        )

    # Handle cancel option
    if request.restore_option == 4:
        return RewindExecuteResponse(
            success=True,
            message="Rewind cancelled",
            checkpoint_index=request.checkpoint_index,
            restore_option=4
        )

    # Get working directory
    working_dir = str(settings.workspace_dir)
    project_id = session.get("project_id")
    if project_id:
        project = database.get_project(project_id)
        if project:
            working_dir = str(settings.workspace_dir / project["path"])

    # Execute rewind via rewind manager
    result = rewind_manager.execute_rewind(
        sdk_session_id=sdk_session_id,
        checkpoint_index=request.checkpoint_index,
        restore_option=request.restore_option,
        working_dir=working_dir
    )

    if result["success"]:
        # Sync our local database if conversation was restored
        if request.restore_option in [1, 2]:
            # Get the checkpoint message for syncing
            checkpoint_message = request.checkpoint_message

            if checkpoint_message:
                # Delete messages after checkpoint in our database
                messages = database.get_session_messages(session_id)
                checkpoint_index = -1

                for i, msg in enumerate(messages):
                    if msg["role"] == "user" and checkpoint_message in msg["content"]:
                        checkpoint_index = i
                        break

                if checkpoint_index >= 0:
                    messages_to_delete = messages[checkpoint_index + 1:]
                    for msg in messages_to_delete:
                        try:
                            database.delete_session_message(session_id, msg["id"])
                        except Exception as e:
                            logger.error(f"Failed to delete message {msg['id']}: {e}")

        return RewindExecuteResponse(
            success=True,
            message=result.get("message", "Rewind completed successfully"),
            checkpoint_index=request.checkpoint_index,
            restore_option=request.restore_option
        )
    else:
        return RewindExecuteResponse(
            success=False,
            message="Rewind failed",
            error=result.get("error", "Unknown error")
        )


@router.get("/rewind/status", response_model=RewindStatus)
async def get_rewind_status():
    """
    Get current rewind status.

    Checks if there's a pending rewind configuration in settings.
    """
    pending = rewind_manager.get_pending_rewind()

    return RewindStatus(
        has_pending=pending is not None,
        pending_rewind=pending
    )


@router.post("/rewind/clear")
async def clear_pending_rewind():
    """
    Clear any pending rewind configuration.
    """
    success = rewind_manager.clear_pending_rewind()

    return {
        "success": success,
        "message": "Pending rewind cleared" if success else "Failed to clear pending rewind"
    }

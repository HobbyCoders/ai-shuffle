"""
Session management API routes
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, status, Request, UploadFile, File
from pydantic import BaseModel

from app.core.models import Session, SessionWithMessages, SessionSearchResult
from app.db import database
from app.api.auth import require_auth, get_api_user_from_request

router = APIRouter(prefix="/api/v1/sessions", tags=["Sessions"])


class BatchDeleteRequest(BaseModel):
    """Request body for batch delete operation"""
    session_ids: List[str]


def check_session_access(request: Request, session: dict) -> None:
    """Check if API user has access to a session based on project/profile restrictions."""
    api_user = get_api_user_from_request(request)
    if not api_user:
        return  # Admin has full access

    # Check project restriction
    if api_user.get("project_id") and session.get("project_id"):
        if api_user["project_id"] != session["project_id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this session"
            )

    # Check profile restriction
    if api_user.get("profile_id"):
        if api_user["profile_id"] != session.get("profile_id"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this session"
            )


def enrich_sessions_with_tags(sessions: List[dict]) -> List[dict]:
    """Add tags array and has_forks field to each session"""
    for session in sessions:
        session["tags"] = database.get_session_tags(session["id"])
        session["has_forks"] = database.session_has_forks(session["id"])
    return sessions


@router.get("", response_model=List[Session])
async def list_sessions(
    request: Request,
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    api_user_id: Optional[str] = Query(None, description="Filter by API user ID (admin only)"),
    admin_only: bool = Query(False, description="Show only admin sessions (no API user)"),
    api_users_only: bool = Query(False, description="Show only API user sessions (exclude admin sessions)"),
    favorites_only: bool = Query(False, description="Show only favorited sessions"),
    tag_id: Optional[str] = Query(None, description="Filter by tag ID"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """List sessions with optional filters. API users only see sessions for their assigned project/profile."""
    api_user = get_api_user_from_request(request)

    # Force API user restrictions
    if api_user:
        if api_user.get("project_id"):
            project_id = api_user["project_id"]
        if api_user.get("profile_id"):
            profile_id = api_user["profile_id"]
        # API users can only see their own sessions
        api_user_id = api_user["id"]
        admin_only = False
        api_users_only = False

    # Determine api_user_id filter value
    # - If admin_only=True, filter for sessions with api_user_id IS NULL
    # - If api_users_only=True, filter for sessions with api_user_id IS NOT NULL
    # - If api_user_id is specified, filter for that specific user
    # - Otherwise, show all sessions
    filter_api_user_id = None
    filter_api_users_only = False
    if admin_only:
        filter_api_user_id = ""  # Empty string signals "IS NULL" in database.get_sessions
    elif api_users_only:
        filter_api_users_only = True
    elif api_user_id:
        filter_api_user_id = api_user_id

    sessions = database.get_sessions(
        project_id=project_id,
        profile_id=profile_id,
        status=status_filter,
        api_user_id=filter_api_user_id,
        api_users_only=filter_api_users_only,
        favorites_only=favorites_only,
        tag_id=tag_id,
        limit=limit,
        offset=offset
    )
    return enrich_sessions_with_tags(sessions)


@router.get("/search/query", response_model=List[SessionSearchResult])
async def search_sessions(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query"),
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    admin_only: bool = Query(False, description="Show only admin sessions"),
    limit: int = Query(20, ge=1, le=50),
    token: str = Depends(require_auth)
):
    """
    Search sessions by title and message content.
    Returns sessions with snippets showing where the match was found.
    API users only see sessions for their assigned project/profile.
    """
    api_user = get_api_user_from_request(request)

    # Force API user restrictions
    filter_api_user_id = None
    if api_user:
        if api_user.get("project_id"):
            project_id = api_user["project_id"]
        if api_user.get("profile_id"):
            profile_id = api_user["profile_id"]
        filter_api_user_id = api_user["id"]
        admin_only = False
    elif admin_only:
        # Admin requesting only admin sessions
        filter_api_user_id = None

    results = database.search_sessions(
        query=q,
        project_id=project_id,
        profile_id=profile_id,
        api_user_id=filter_api_user_id,
        admin_only=admin_only,
        limit=limit
    )

    return results


@router.get("/{session_id}", response_model=SessionWithMessages)
async def get_session(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Get a session with its message history. API users can only access their assigned sessions."""
    import logging
    import traceback
    logger = logging.getLogger(__name__)

    logger.info(f"Loading session: {session_id}")

    session = database.get_session(session_id)
    if not session:
        logger.warning(f"Session not found in database: {session_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, session)

    # Try to load messages from JSONL file first (source of truth for consistency)
    sdk_session_id = session.get("sdk_session_id")
    messages = []
    working_dir = "/workspace"

    if sdk_session_id:
        try:
            from app.core.jsonl_parser import parse_session_history, get_session_cost_from_jsonl
            from app.core.config import settings

            # Get working dir from project if available
            project_id = session.get("project_id")
            if project_id:
                project = database.get_project(project_id)
                if project:
                    working_dir = str(settings.workspace_dir / project["path"])

            jsonl_messages = parse_session_history(sdk_session_id, working_dir)
            if jsonl_messages:
                # Transform to expected format for SessionWithMessages
                # Use camelCase for frontend compatibility (toolName, toolInput, toolId)
                for i, m in enumerate(jsonl_messages):
                    # Get timestamp from metadata, ensuring it's properly formatted
                    timestamp = m.get("metadata", {}).get("timestamp")
                    # Don't pass raw timestamp strings to Pydantic datetime field
                    # The frontend handles timestamp display from metadata anyway
                    msg_data = {
                        "id": m.get("id", i),
                        "role": m.get("role", "user"),
                        "content": m.get("content", ""),
                        "type": m.get("type"),  # Critical for tool_use/tool_result rendering
                        "subtype": m.get("subtype"),  # For system messages (e.g., local_command)
                        "toolName": m.get("toolName"),  # camelCase for frontend
                        "toolInput": m.get("toolInput"),  # camelCase for frontend
                        "toolId": m.get("toolId"),
                        "toolResult": m.get("toolResult"),  # Tool output grouped with tool_use
                        "toolStatus": m.get("toolStatus"),  # Status: running, complete, error
                        "tool_name": m.get("toolName"),  # Also include snake_case for compatibility
                        "tool_input": m.get("toolInput"),  # Also include snake_case for compatibility
                        "metadata": m.get("metadata"),
                        "created_at": None  # Let Pydantic use default; timestamp is in metadata
                    }
                    # Include subagent-specific fields if present
                    if m.get("type") == "subagent":
                        msg_data["agentId"] = m.get("agentId")
                        msg_data["agentType"] = m.get("agentType")
                        msg_data["agentDescription"] = m.get("agentDescription")
                        msg_data["agentStatus"] = m.get("agentStatus")
                        msg_data["agentChildren"] = m.get("agentChildren")
                    messages.append(msg_data)

            # Get token usage from JSONL - always load cache tokens since they're not in DB
            # Also load input/output tokens if database doesn't have them
            usage_data = get_session_cost_from_jsonl(sdk_session_id, working_dir)
            logger.info(f"[Session API] JSONL usage_data for {session_id}: {usage_data}")
            if usage_data:
                # Cache tokens are only in JSONL, always use those
                cache_creation = usage_data.get("cache_creation_tokens", 0)
                cache_read = usage_data.get("cache_read_tokens", 0)
                last_input = usage_data.get("last_input_tokens", 0)

                session["cache_creation_tokens"] = cache_creation
                session["cache_read_tokens"] = cache_read
                # Context window = last turn's input + cache creation + cache read
                session["context_tokens"] = last_input + cache_creation + cache_read

                # Input/output tokens - use JSONL if DB doesn't have them
                if session.get("total_tokens_in", 0) == 0 and session.get("total_tokens_out", 0) == 0:
                    session["total_tokens_in"] = usage_data.get("total_tokens_in", 0)
                    session["total_tokens_out"] = usage_data.get("total_tokens_out", 0)
            logger.info(f"[Session API] Final token values: tokens_in={session.get('total_tokens_in')}, tokens_out={session.get('total_tokens_out')}, cache_creation={session.get('cache_creation_tokens')}, cache_read={session.get('cache_read_tokens')}, context={session.get('context_tokens')}")
        except Exception as e:
            # Log the error but don't fail - fall back to database
            logger.error(f"Failed to parse JSONL for session {session_id}: {e}")
            messages = []

    # Fall back to database if JSONL not available or failed to parse
    if not messages:
        db_messages = database.get_session_messages(session_id)
        # Transform DB messages to include type field for frontend compatibility
        for m in db_messages:
            msg = dict(m)
            # Infer type from role for legacy DB messages
            if msg.get("tool_name"):
                msg["type"] = "tool_use"
                msg["toolName"] = msg.get("tool_name")
                msg["toolInput"] = msg.get("tool_input")
            elif msg.get("role") == "assistant":
                msg["type"] = "text"
            messages.append(msg)

    session["messages"] = messages
    session["tags"] = database.get_session_tags(session_id)
    session["has_forks"] = database.session_has_forks(session_id)

    logger.info(f"Returning session {session_id} with {len(messages)} messages")
    try:
        return session
    except Exception as e:
        logger.error(f"Failed to serialize session {session_id}: {e}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to serialize session: {str(e)}"
        )


@router.patch("/{session_id}")
async def update_session(
    request: Request,
    session_id: str,
    title: Optional[str] = None,
    session_status: Optional[str] = Query(None, alias="status"),
    token: str = Depends(require_auth)
):
    """Update session title or status. API users can only modify their accessible sessions."""
    existing = database.get_session(session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

    session = database.update_session(
        session_id=session_id,
        title=title,
        status=session_status
    )

    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Delete a session and its messages. API users can only delete their accessible sessions."""
    existing = database.get_session(session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

    database.delete_session(session_id)


@router.post("/batch-delete", status_code=status.HTTP_200_OK)
async def batch_delete_sessions(
    request: Request,
    body: BatchDeleteRequest,
    token: str = Depends(require_auth)
):
    """
    Delete multiple sessions at once.
    API users can only delete sessions they have access to.
    Returns count of successfully deleted sessions.
    """
    deleted_count = 0
    errors = []

    for session_id in body.session_ids:
        try:
            existing = database.get_session(session_id)
            if not existing:
                errors.append(f"Session not found: {session_id}")
                continue

            check_session_access(request, existing)
            database.delete_session(session_id)
            deleted_count += 1
        except HTTPException as e:
            errors.append(f"Access denied for session {session_id}: {e.detail}")
        except Exception as e:
            errors.append(f"Error deleting session {session_id}: {str(e)}")

    return {
        "deleted_count": deleted_count,
        "total_requested": len(body.session_ids),
        "errors": errors if errors else None
    }


@router.post("/{session_id}/archive")
async def archive_session(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Archive a session. API users can only archive their accessible sessions."""
    existing = database.get_session(session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

    session = database.update_session(
        session_id=session_id,
        status="archived"
    )

    return {"status": "ok", "message": "Session archived"}


@router.patch("/{session_id}/favorite", response_model=Session)
async def toggle_session_favorite(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Toggle the favorite status of a session. Returns the updated session."""
    existing = database.get_session(session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

    session = database.toggle_session_favorite(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle favorite status"
        )

    return session


# ============================================================================
# Sync endpoints for cross-device synchronization (polling fallback)
# ============================================================================

@router.get("/{session_id}/sync")
async def get_sync_changes(
    request: Request,
    session_id: str,
    since_id: int = Query(0, description="Get changes after this sync ID"),
    token: str = Depends(require_auth)
):
    """
    Get sync changes for a session since a specific sync ID.
    Used as a polling fallback when WebSocket is unavailable.

    Returns:
        - changes: List of sync events since since_id
        - latest_id: The most recent sync ID (use for next poll)
        - is_streaming: Whether the session is currently streaming
    """
    existing = database.get_session(session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, existing)

    changes = database.get_sync_logs(session_id, since_id=since_id)
    latest_id = database.get_latest_sync_id(session_id)

    # Check if streaming (import here to avoid circular import)
    from app.core.sync_engine import sync_engine
    is_streaming = sync_engine.is_session_streaming(session_id)

    return {
        "changes": changes,
        "latest_id": latest_id,
        "is_streaming": is_streaming,
        "connected_devices": sync_engine.get_device_count(session_id)
    }


@router.get("/{session_id}/state")
async def get_session_state(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth)
):
    """
    Get current session state for client reconnection.

    This endpoint is used by clients to check the session state when reconnecting,
    including whether work is in progress and if there are buffered streaming messages.

    Returns:
        - session_id: The session ID
        - title: Session title
        - status: Session status (active, archived, etc.)
        - is_streaming: Whether work is currently in progress
        - connected_devices: Number of devices watching this session
        - streaming_messages: Buffered messages if streaming (for late-joiners)
    """
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, session)

    # Get current streaming state from SyncEngine
    from app.core.sync_engine import sync_engine
    sync_state = await sync_engine.get_session_state(session_id)

    # Combine session metadata with streaming state
    return {
        "session_id": session_id,
        "title": session.get("title"),
        "status": session.get("status"),
        "is_streaming": sync_state["is_streaming"],
        "connected_devices": sync_state["connected_devices"],
        "streaming_messages": sync_state.get("streaming_messages", [])
    }


@router.get("/{session_id}/export")
async def export_session(
    request: Request,
    session_id: str,
    format: str = Query("markdown", description="Export format: markdown or json"),
    token: str = Depends(require_auth)
):
    """
    Export a session in the specified format.

    Args:
        session_id: The session ID to export
        format: Export format - 'markdown' or 'json'

    Returns:
        - For markdown: Plain text Markdown representation
        - For JSON: Full session data including metadata and messages
    """
    import logging
    from datetime import datetime
    from fastapi.responses import Response
    import json

    logger = logging.getLogger(__name__)

    # Validate format
    if format not in ("markdown", "json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid format. Must be 'markdown' or 'json'"
        )

    session = database.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, session)

    # Load messages from JSONL (same logic as get_session)
    sdk_session_id = session.get("sdk_session_id")
    messages = []
    working_dir = "/workspace"

    if sdk_session_id:
        try:
            from app.core.jsonl_parser import parse_session_history
            from app.core.config import settings

            # Get working dir from project if available
            project_id = session.get("project_id")
            if project_id:
                project = database.get_project(project_id)
                if project:
                    working_dir = str(settings.workspace_dir / project["path"])

            jsonl_messages = parse_session_history(sdk_session_id, working_dir)
            if jsonl_messages:
                messages = jsonl_messages
        except Exception as e:
            logger.error(f"Failed to parse JSONL for session {session_id}: {e}")
            messages = []

    # Fall back to database if JSONL not available
    if not messages:
        db_messages = database.get_session_messages(session_id)
        for m in db_messages:
            msg = dict(m)
            if msg.get("tool_name"):
                msg["type"] = "tool_use"
                msg["toolName"] = msg.get("tool_name")
                msg["toolInput"] = msg.get("tool_input")
            elif msg.get("role") == "assistant":
                msg["type"] = "text"
            messages.append(msg)

    # Generate filename
    title = session.get("title") or "untitled-session"
    # Sanitize title for filename
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "" for c in title)
    safe_title = safe_title.strip().replace(" ", "-")[:50]
    date_str = datetime.now().strftime("%Y%m%d")

    if format == "json":
        # JSON export - include full session data
        export_data = {
            "session": {
                "id": session.get("id"),
                "title": session.get("title"),
                "status": session.get("status"),
                "profile_id": session.get("profile_id"),
                "project_id": session.get("project_id"),
                "created_at": session.get("created_at"),
                "updated_at": session.get("updated_at"),
                "total_cost_usd": session.get("total_cost_usd"),
                "total_tokens_in": session.get("total_tokens_in"),
                "total_tokens_out": session.get("total_tokens_out"),
                "turn_count": session.get("turn_count"),
            },
            "messages": messages,
            "exported_at": datetime.now().isoformat(),
            "format_version": "1.0"
        }

        filename = f"{safe_title}-{date_str}.json"
        content = json.dumps(export_data, indent=2, default=str)

        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )

    else:
        # Markdown export
        md_lines = []

        # Header
        md_lines.append(f"# {session.get('title') or 'Untitled Session'}")
        md_lines.append("")

        # Metadata
        md_lines.append("## Session Info")
        md_lines.append("")
        md_lines.append(f"- **Created:** {session.get('created_at')}")
        md_lines.append(f"- **Updated:** {session.get('updated_at')}")
        if session.get("total_cost_usd"):
            md_lines.append(f"- **Cost:** ${session.get('total_cost_usd'):.4f}")
        if session.get("turn_count"):
            md_lines.append(f"- **Turns:** {session.get('turn_count')}")
        md_lines.append("")

        # Messages
        md_lines.append("## Conversation")
        md_lines.append("")

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            msg_type = msg.get("type")
            timestamp = msg.get("metadata", {}).get("timestamp") if isinstance(msg.get("metadata"), dict) else None

            # Skip empty messages and certain system messages
            if not content and msg_type not in ("tool_use", "subagent"):
                continue

            if role == "user":
                md_lines.append("### Human")
                if timestamp:
                    md_lines.append(f"*{timestamp}*")
                md_lines.append("")
                md_lines.append(content)
                md_lines.append("")

            elif role == "assistant":
                if msg_type == "tool_use":
                    tool_name = msg.get("toolName") or msg.get("tool_name", "Unknown Tool")
                    tool_input = msg.get("toolInput") or msg.get("tool_input", {})
                    tool_result = msg.get("toolResult", "")

                    md_lines.append(f"### Tool: {tool_name}")
                    if timestamp:
                        md_lines.append(f"*{timestamp}*")
                    md_lines.append("")

                    # Format tool input
                    if tool_input:
                        md_lines.append("**Input:**")
                        md_lines.append("```json")
                        md_lines.append(json.dumps(tool_input, indent=2, default=str))
                        md_lines.append("```")
                        md_lines.append("")

                    # Format tool result
                    if tool_result:
                        md_lines.append("**Result:**")
                        # Check if result looks like code or file content
                        if "\n" in tool_result or len(tool_result) > 100:
                            md_lines.append("```")
                            md_lines.append(tool_result)
                            md_lines.append("```")
                        else:
                            md_lines.append(tool_result)
                        md_lines.append("")

                elif msg_type == "subagent":
                    agent_type = msg.get("agentType", "Agent")
                    agent_desc = msg.get("agentDescription", "")

                    md_lines.append(f"### Subagent: {agent_type}")
                    if timestamp:
                        md_lines.append(f"*{timestamp}*")
                    md_lines.append("")
                    if agent_desc:
                        md_lines.append(f"**Task:** {agent_desc}")
                        md_lines.append("")
                    if content:
                        md_lines.append(content)
                        md_lines.append("")

                else:
                    # Regular text message
                    md_lines.append("### Assistant")
                    if timestamp:
                        md_lines.append(f"*{timestamp}*")
                    md_lines.append("")
                    md_lines.append(content)
                    md_lines.append("")

            elif role == "system":
                # Include system messages as notes
                subtype = msg.get("subtype", "")
                if content:
                    md_lines.append(f"---")
                    md_lines.append(f"*System ({subtype}):* {content[:500]}")
                    md_lines.append(f"---")
                    md_lines.append("")

        # Footer
        md_lines.append("---")
        md_lines.append(f"*Exported from AI Hub on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        filename = f"{safe_title}-{date_str}.md"
        content = "\n".join(md_lines)

        return Response(
            content=content,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )


class SessionImportData(BaseModel):
    """Data structure for imported session"""
    session: dict
    messages: List[dict]
    exported_at: Optional[str] = None
    format_version: Optional[str] = None


class SessionImportResponse(BaseModel):
    """Response for session import"""
    session_id: str
    title: Optional[str]
    message_count: int
    status: str


@router.post("/import", response_model=SessionImportResponse)
async def import_session(
    request: Request,
    file: UploadFile = File(...),
    token: str = Depends(require_auth)
):
    """
    Import a session from a JSON file export.

    Accepts AI Hub JSON export format with session metadata and messages.
    Creates a new session with a new ID to avoid conflicts.

    Args:
        file: JSON file containing exported session data

    Returns:
        Information about the imported session
    """
    import logging
    import json
    import uuid
    from datetime import datetime
    from pathlib import Path
    from app.core.config import settings

    logger = logging.getLogger(__name__)

    # Validate file type
    if not file.filename or not file.filename.endswith('.json'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Please upload a JSON file."
        )

    # Read and parse file content
    try:
        content = await file.read()
        if len(content) > 50 * 1024 * 1024:  # 50MB limit
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File too large. Maximum size is 50MB."
            )

        import_data = json.loads(content.decode('utf-8'))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON file: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to read file: {str(e)}"
        )

    # Validate import data structure
    if 'session' not in import_data or 'messages' not in import_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid export format. Missing 'session' or 'messages' field."
        )

    session_data = import_data['session']
    messages_data = import_data['messages']

    if not isinstance(session_data, dict) or not isinstance(messages_data, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid export format. 'session' must be an object and 'messages' must be an array."
        )

    # Generate new session ID
    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"
    new_sdk_session_id = str(uuid.uuid4())

    # Get profile ID - use from import or default to 'default'
    profile_id = session_data.get('profile_id') or 'default'

    # Verify profile exists, fall back to default if not
    profile = database.get_profile(profile_id)
    if not profile:
        profile_id = 'default'
        profile = database.get_profile(profile_id)
        if not profile:
            # Create default profile if it doesn't exist
            profiles = database.get_all_profiles()
            if profiles:
                profile_id = profiles[0]['id']
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No profiles available. Please create a profile first."
                )

    # Get API user if applicable
    api_user = get_api_user_from_request(request)
    api_user_id = api_user['id'] if api_user else None

    # Create session in database
    title = session_data.get('title') or 'Imported Session'
    now = datetime.utcnow().isoformat()

    session = database.create_session(
        session_id=new_session_id,
        profile_id=profile_id,
        project_id=session_data.get('project_id'),
        title=title,
        api_user_id=api_user_id
    )

    # Update session with SDK session ID and stats
    database.update_session(
        session_id=new_session_id,
        sdk_session_id=new_sdk_session_id,
        cost_increment=session_data.get('total_cost_usd') or 0,
        tokens_in_increment=session_data.get('total_tokens_in') or 0,
        tokens_out_increment=session_data.get('total_tokens_out') or 0,
        turn_increment=session_data.get('turn_count') or len([m for m in messages_data if m.get('role') == 'user'])
    )

    # Create JSONL file for the session
    try:
        # Get Claude projects directory
        claude_projects_dir = settings.get_claude_projects_dir

        # Determine project directory - use /workspace by default
        working_dir = "/workspace"
        if session_data.get('project_id'):
            project = database.get_project(session_data['project_id'])
            if project:
                working_dir = str(settings.workspace_dir / project["path"])

        # Convert working dir to Claude's project directory format
        project_dir_name = working_dir.replace("/", "-")
        project_dir = claude_projects_dir / project_dir_name

        # Ensure project directory exists
        project_dir.mkdir(parents=True, exist_ok=True)

        jsonl_path = project_dir / f"{new_sdk_session_id}.jsonl"

        # Convert messages to JSONL format
        with open(jsonl_path, 'w', encoding='utf-8') as f:
            for msg in messages_data:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                msg_type = msg.get('type')
                timestamp = msg.get('metadata', {}).get('timestamp') or now
                msg_uuid = msg.get('id') or str(uuid.uuid4())

                if role == 'user' and msg_type != 'tool_result':
                    # User message
                    entry = {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": content
                        },
                        "uuid": msg_uuid,
                        "timestamp": timestamp
                    }
                    f.write(json.dumps(entry) + '\n')

                elif role == 'assistant':
                    if msg_type == 'tool_use':
                        # Tool use message
                        tool_name = msg.get('toolName') or msg.get('tool_name')
                        tool_input = msg.get('toolInput') or msg.get('tool_input') or {}
                        tool_id = msg.get('toolId') or msg.get('tool_id') or str(uuid.uuid4())

                        content_blocks = []
                        if content:
                            content_blocks.append({"type": "text", "text": content})
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": tool_input
                        })

                        entry = {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": content_blocks
                            },
                            "uuid": msg_uuid,
                            "timestamp": timestamp
                        }
                        f.write(json.dumps(entry) + '\n')

                        # Write tool result if present
                        tool_result = msg.get('toolResult')
                        if tool_result:
                            result_entry = {
                                "type": "user",
                                "message": {
                                    "role": "user",
                                    "content": [{
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "content": tool_result
                                    }]
                                },
                                "uuid": str(uuid.uuid4()),
                                "timestamp": timestamp
                            }
                            f.write(json.dumps(result_entry) + '\n')

                    elif msg_type == 'subagent':
                        # Subagent message - write as tool use
                        tool_id = msg.get('toolId') or str(uuid.uuid4())
                        agent_type = msg.get('agentType', 'unknown')
                        description = msg.get('agentDescription', '')

                        content_blocks = [{
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Task",
                            "input": {
                                "subagent_type": agent_type,
                                "description": description
                            }
                        }]

                        entry = {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": content_blocks
                            },
                            "uuid": msg_uuid,
                            "timestamp": timestamp
                        }
                        f.write(json.dumps(entry) + '\n')

                    else:
                        # Text message
                        if content:
                            entry = {
                                "type": "assistant",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": content}]
                                },
                                "uuid": msg_uuid,
                                "timestamp": timestamp
                            }
                            f.write(json.dumps(entry) + '\n')

                elif role == 'system':
                    # System message
                    subtype = msg.get('subtype', '')
                    entry = {
                        "type": "system",
                        "subtype": subtype,
                        "content": content,
                        "uuid": msg_uuid,
                        "timestamp": timestamp
                    }
                    f.write(json.dumps(entry) + '\n')

        logger.info(f"Created JSONL file for imported session: {jsonl_path}")

    except Exception as e:
        logger.error(f"Failed to create JSONL file for imported session: {e}")
        # Continue anyway - the session is in the database

    logger.info(f"Imported session {new_session_id} with {len(messages_data)} messages")

    return SessionImportResponse(
        session_id=new_session_id,
        title=title,
        message_count=len(messages_data),
        status="success"
    )


# ============================================================================
# Session Fork Endpoint
# ============================================================================

class ForkSessionRequest(BaseModel):
    """Request body for forking a session"""
    message_index: int


class ForkSessionResponse(BaseModel):
    """Response for session fork"""
    session_id: str
    title: Optional[str]
    parent_session_id: str
    fork_point_message_index: int
    message_count: int
    status: str


@router.post("/{session_id}/fork", response_model=ForkSessionResponse)
async def fork_session(
    request: Request,
    session_id: str,
    body: ForkSessionRequest,
    token: str = Depends(require_auth)
):
    """
    Fork a session from a specific message index.

    Creates a new session that contains messages up to and including the
    specified message index. The new session references the original as its parent.

    Args:
        session_id: The session ID to fork from
        body: Request body containing message_index (0-based index to fork after)

    Returns:
        Information about the forked session
    """
    import logging
    import uuid
    import json
    from pathlib import Path
    from app.core.config import settings
    from app.core.jsonl_parser import get_session_jsonl_path, parse_jsonl_file

    logger = logging.getLogger(__name__)

    # Get the original session
    original_session = database.get_session(session_id)
    if not original_session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, original_session)

    sdk_session_id = original_session.get("sdk_session_id")
    if not sdk_session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot fork session without SDK session ID"
        )

    # Get working directory
    working_dir = "/workspace"
    project_id = original_session.get("project_id")
    if project_id:
        project = database.get_project(project_id)
        if project:
            working_dir = str(settings.workspace_dir / project["path"])

    # Find the original JSONL file
    original_jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not original_jsonl_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot fork session: JSONL file not found"
        )

    # Parse original JSONL to get messages up to fork point
    original_entries = list(parse_jsonl_file(original_jsonl_path))

    # Count actual display messages (filter out queue-operation, file-history-snapshot, etc.)
    display_entries = []
    for entry in original_entries:
        entry_type = entry.get("type")
        # Skip non-message entries
        if entry_type in ("queue-operation", "file-history-snapshot"):
            continue
        # Skip meta messages
        if entry.get("isMeta"):
            continue
        # Skip sidechain messages
        if entry.get("isSidechain"):
            continue
        display_entries.append(entry)

    # Validate message_index
    if body.message_index < 0 or body.message_index >= len(display_entries):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid message_index: {body.message_index}. Session has {len(display_entries)} messages (0-{len(display_entries)-1})"
        )

    # Find the actual index in original_entries that corresponds to the message_index in display_entries
    display_count = 0
    fork_entry_index = 0
    for i, entry in enumerate(original_entries):
        entry_type = entry.get("type")
        if entry_type in ("queue-operation", "file-history-snapshot"):
            continue
        if entry.get("isMeta"):
            continue
        if entry.get("isSidechain"):
            continue

        if display_count == body.message_index:
            fork_entry_index = i
            break
        display_count += 1

    # Generate new IDs
    new_session_id = f"ses-{uuid.uuid4().hex[:16]}"
    new_sdk_session_id = str(uuid.uuid4())

    # Get API user if applicable
    api_user = get_api_user_from_request(request)
    api_user_id = api_user['id'] if api_user else None

    # Create title for fork
    original_title = original_session.get('title') or 'Untitled'
    fork_title = f"Fork of {original_title}"

    # Create the forked session in database
    forked_session = database.create_session(
        session_id=new_session_id,
        profile_id=original_session.get('profile_id'),
        project_id=original_session.get('project_id'),
        title=fork_title,
        api_user_id=api_user_id,
        parent_session_id=session_id,
        fork_point_message_index=body.message_index
    )

    # Update with SDK session ID
    database.update_session(
        session_id=new_session_id,
        sdk_session_id=new_sdk_session_id
    )

    # Create new JSONL file with entries up to and including fork point
    try:
        # Get Claude projects directory
        claude_projects_dir = settings.get_claude_projects_dir

        # Convert working dir to Claude's project directory format
        project_dir_name = working_dir.replace("/", "-")
        project_dir = claude_projects_dir / project_dir_name

        # Ensure project directory exists
        project_dir.mkdir(parents=True, exist_ok=True)

        new_jsonl_path = project_dir / f"{new_sdk_session_id}.jsonl"

        # Write entries up to and including fork point
        with open(new_jsonl_path, 'w', encoding='utf-8') as f:
            for i, entry in enumerate(original_entries):
                if i > fork_entry_index:
                    break
                f.write(json.dumps(entry) + '\n')

        logger.info(f"Created forked JSONL file: {new_jsonl_path} with {fork_entry_index + 1} entries")

    except Exception as e:
        logger.error(f"Failed to create JSONL file for forked session: {e}")
        # Delete the session we just created since fork failed
        database.delete_session(new_session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create forked session: {str(e)}"
        )

    logger.info(f"Forked session {session_id} to {new_session_id} at message index {body.message_index}")

    return ForkSessionResponse(
        session_id=new_session_id,
        title=fork_title,
        parent_session_id=session_id,
        fork_point_message_index=body.message_index,
        message_count=fork_entry_index + 1,
        status="success"
    )


# ============================================================================
# Worktree Session Endpoint
# ============================================================================

class CreateWorktreeSessionRequest(BaseModel):
    """Request body for creating a session with a worktree"""
    project_id: str
    branch_name: str
    base_branch: str = "main"
    profile_id: Optional[str] = None


class WorktreeSessionResponse(BaseModel):
    """Response for worktree session creation"""
    session_id: str
    worktree_id: str
    branch_name: str
    base_branch: str
    worktree_path: str
    status: str


@router.post("/with-worktree", response_model=WorktreeSessionResponse)
async def create_session_with_worktree(
    request: Request,
    body: CreateWorktreeSessionRequest,
    token: str = Depends(require_auth)
):
    """
    Create a new session with an associated git worktree.

    This endpoint creates both a git worktree (for isolated branch development)
    and a linked chat session. The worktree allows parallel development on a
    feature branch while keeping the main repository intact.

    Args:
        project_id: The project to create the worktree in
        branch_name: Name for the new branch
        base_branch: Branch to base the new branch on (default: main)
        profile_id: Optional profile ID for the session

    Returns:
        Session and worktree information
    """
    import logging
    from app.core.worktree_manager import worktree_manager

    logger = logging.getLogger(__name__)

    # Verify project exists and user has access
    project = database.get_project(body.project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {body.project_id}"
        )

    # Check API user restrictions
    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id"):
        if api_user["project_id"] != body.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project"
            )

    # Create worktree and session
    from app.core.worktree_manager import WorktreeError

    try:
        worktree, session = worktree_manager.create_worktree_session(
            project_id=body.project_id,
            branch_name=body.branch_name,
            create_new_branch=True,
            base_branch=body.base_branch,
            profile_id=body.profile_id
        )
    except WorktreeError as e:
        logger.warning(f"Worktree creation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Unexpected error creating worktree: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )

    logger.info(f"Created worktree session: {session['id']} with worktree: {worktree['id']}")

    return WorktreeSessionResponse(
        session_id=session["id"],
        worktree_id=worktree["id"],
        branch_name=worktree["branch_name"],
        base_branch=worktree.get("base_branch") or body.base_branch,
        worktree_path=worktree["worktree_path"],
        status="active"
    )


# ============================================================================
# Active Sessions Management (running SDK clients)
# ============================================================================

class ActiveSessionInfo(BaseModel):
    """Info about an active session with a running SDK client"""
    session_id: str
    is_connected: bool
    is_streaming: bool
    last_activity: Optional[str]
    sdk_session_id: Optional[str]
    title: Optional[str]
    project_id: Optional[str]
    worktree_id: Optional[str]


class CloseSessionResponse(BaseModel):
    """Response for closing an active session"""
    success: bool
    message: str


@router.get("/active", response_model=List[ActiveSessionInfo])
async def list_active_sessions(
    request: Request,
    token: str = Depends(require_auth)
):
    """
    List all active sessions with running SDK clients.

    Active sessions have a connected Claude CLI process that may be holding
    file handles on their working directory. This is useful for:
    - Seeing which sessions are currently in use
    - Identifying sessions that need to be closed before deleting worktrees
    - Monitoring system resource usage
    """
    from app.core.query_engine import get_active_sessions_info

    active = get_active_sessions_info()
    return [ActiveSessionInfo(**info) for info in active]


@router.post("/active/{session_id}/close", response_model=CloseSessionResponse)
async def close_active_session(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth)
):
    """
    Close an active session, disconnecting its SDK client.

    This releases any file handles the Claude CLI process has on the working
    directory, allowing the directory (e.g., a worktree) to be deleted.

    The session data (messages, history) is preserved - only the running
    process is terminated. The session can be resumed later.
    """
    from app.core.query_engine import close_active_session as close_session

    success = await close_session(session_id)

    if success:
        return CloseSessionResponse(
            success=True,
            message=f"Session {session_id} closed successfully"
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} is not currently active"
        )

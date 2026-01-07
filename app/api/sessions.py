"""
Session management API routes
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, status, Request

from app.core.models import Session, SessionWithMessages
from app.db import database
from app.api.auth import require_auth, get_api_user_from_request

router = APIRouter(prefix="/api/v1/sessions", tags=["Sessions"])


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


@router.get("", response_model=List[Session])
async def list_sessions(
    request: Request,
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    api_user_id: Optional[str] = Query(None, description="Filter by API user ID (admin only)"),
    admin_only: bool = Query(False, description="Show only admin sessions (no API user)"),
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

    # Determine api_user_id filter value
    # - If admin_only=True, filter for sessions with api_user_id IS NULL
    # - If api_user_id is specified, filter for that specific user
    # - Otherwise, show all sessions
    filter_api_user_id = None
    if admin_only:
        filter_api_user_id = ""  # Empty string signals "IS NULL" in database.get_sessions
    elif api_user_id:
        filter_api_user_id = api_user_id

    sessions = database.get_sessions(
        project_id=project_id,
        profile_id=profile_id,
        status=status_filter,
        api_user_id=filter_api_user_id,
        limit=limit,
        offset=offset
    )
    return sessions


@router.get("/{session_id}", response_model=SessionWithMessages)
async def get_session(request: Request, session_id: str, token: str = Depends(require_auth)):
    """Get a session with its message history. API users can only access their assigned sessions."""
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, session)

    messages = database.get_session_messages(session_id)
    session["messages"] = messages

    return session


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
    Get full session state for initial sync.
    Called when a device first connects to get caught up.

    Returns:
        - session: Session metadata
        - messages: All messages in the session
        - is_streaming: Whether the session is currently streaming
        - latest_sync_id: Current sync ID for subsequent polling
    """
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    check_session_access(request, session)

    messages = database.get_session_messages(session_id)
    latest_id = database.get_latest_sync_id(session_id)

    from app.core.sync_engine import sync_engine
    is_streaming = sync_engine.is_session_streaming(session_id)

    return {
        "session": session,
        "messages": messages,
        "is_streaming": is_streaming,
        "latest_sync_id": latest_id,
        "connected_devices": sync_engine.get_device_count(session_id)
    }

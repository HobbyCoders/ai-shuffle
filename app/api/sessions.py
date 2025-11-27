"""
Session management API routes
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, status

from app.core.models import Session, SessionWithMessages
from app.db import database
from app.api.auth import require_auth

router = APIRouter(prefix="/api/v1/sessions", tags=["Sessions"])


@router.get("", response_model=List[Session])
async def list_sessions(
    project_id: Optional[str] = Query(None, description="Filter by project"),
    profile_id: Optional[str] = Query(None, description="Filter by profile"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """List sessions with optional filters"""
    sessions = database.get_sessions(
        project_id=project_id,
        profile_id=profile_id,
        status=status_filter,
        limit=limit,
        offset=offset
    )
    return sessions


@router.get("/{session_id}", response_model=SessionWithMessages)
async def get_session(session_id: str, token: str = Depends(require_auth)):
    """Get a session with its message history"""
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    messages = database.get_session_messages(session_id)
    session["messages"] = messages

    return session


@router.patch("/{session_id}")
async def update_session(
    session_id: str,
    title: Optional[str] = None,
    session_status: Optional[str] = Query(None, alias="status"),
    token: str = Depends(require_auth)
):
    """Update session title or status"""
    existing = database.get_session(session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    session = database.update_session(
        session_id=session_id,
        title=title,
        status=session_status
    )

    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str, token: str = Depends(require_auth)):
    """Delete a session and its messages"""
    existing = database.get_session(session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    database.delete_session(session_id)


@router.post("/{session_id}/archive")
async def archive_session(session_id: str, token: str = Depends(require_auth)):
    """Archive a session"""
    existing = database.get_session(session_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )

    session = database.update_session(
        session_id=session_id,
        status="archived"
    )

    return {"status": "ok", "message": "Session archived"}

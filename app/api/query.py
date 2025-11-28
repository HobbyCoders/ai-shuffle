"""
Query API routes - main AI interface
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.responses import StreamingResponse

from app.core.models import (
    QueryRequest, QueryResponse, ConversationRequest, QueryMetadata
)
from app.core.query_engine import execute_query, stream_query, interrupt_session, get_active_sessions, start_background_query
from app.core.auth import auth_service
from app.api.auth import require_auth, get_api_user_from_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Query"])


def require_claude_auth():
    """Dependency that requires Claude CLI authentication"""
    if not auth_service.is_claude_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Claude CLI not authenticated. Please run 'claude login' in the container."
        )


@router.post("/query", response_model=QueryResponse)
async def one_shot_query(
    request: QueryRequest,
    token: str = Depends(require_auth),
    _: None = Depends(require_claude_auth)
):
    """
    One-shot query - stateless, creates a new session each time.
    Best for simple queries that don't need conversation history.
    """
    try:
        overrides = None
        if request.overrides:
            overrides = request.overrides.model_dump(exclude_none=True)

        result = await execute_query(
            prompt=request.prompt,
            profile_id=request.profile,
            project_id=request.project,
            overrides=overrides
        )

        return {
            "response": result["response"],
            "session_id": result["session_id"],
            "metadata": QueryMetadata(**result["metadata"])
        }

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Query error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query execution failed: {str(e)}"
        )


@router.post("/query/stream")
async def stream_one_shot_query(
    request: QueryRequest,
    token: str = Depends(require_auth),
    _: None = Depends(require_claude_auth)
):
    """
    SSE streaming one-shot query.
    """
    async def event_generator():
        try:
            overrides = None
            if request.overrides:
                overrides = request.overrides.model_dump(exclude_none=True)

            async for event in stream_query(
                prompt=request.prompt,
                profile_id=request.profile,
                project_id=request.project,
                overrides=overrides
            ):
                event_type = event.get("type", "message")
                data = json.dumps(event)
                yield f"event: {event_type}\ndata: {data}\n\n"

        except Exception as e:
            logger.error(f"Stream error: {e}")
            error_data = json.dumps({"type": "error", "message": str(e)})
            yield f"event: error\ndata: {error_data}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/conversation", response_model=QueryResponse)
async def conversation(
    request: ConversationRequest,
    token: str = Depends(require_auth),
    _: None = Depends(require_claude_auth)
):
    """
    Multi-turn conversation - maintains context across messages.
    If session_id is provided, continues that session.
    Otherwise creates a new session.
    """
    try:
        overrides = None
        if request.overrides:
            overrides = request.overrides.model_dump(exclude_none=True)

        result = await execute_query(
            prompt=request.prompt,
            profile_id=request.profile or "claude-code",
            project_id=request.project,
            overrides=overrides,
            session_id=request.session_id
        )

        return {
            "response": result["response"],
            "session_id": result["session_id"],
            "metadata": QueryMetadata(**result["metadata"])
        }

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Conversation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Conversation failed: {str(e)}"
        )


@router.post("/conversation/stream")
async def stream_conversation(
    request: ConversationRequest,
    http_request: Request,
    token: str = Depends(require_auth),
    _: None = Depends(require_claude_auth)
):
    """
    SSE streaming conversation.
    When authenticated via API key, uses the API user's configured project and profile.
    """
    # Get API user if authenticated via API key
    api_user = get_api_user_from_request(http_request)

    # Determine profile and project - API user config overrides request
    if api_user:
        # API users use their configured project/profile, request values ignored
        profile_id = api_user.get("profile_id") or request.profile or "claude-code"
        project_id = api_user.get("project_id") or request.project
        api_user_id = api_user.get("id")
    else:
        profile_id = request.profile or "claude-code"
        project_id = request.project
        api_user_id = None

    async def event_generator():
        try:
            overrides = None
            if request.overrides:
                overrides = request.overrides.model_dump(exclude_none=True)

            async for event in stream_query(
                prompt=request.prompt,
                profile_id=profile_id,
                project_id=project_id,
                overrides=overrides,
                session_id=request.session_id,
                api_user_id=api_user_id,
                device_id=request.device_id  # Pass device ID for cross-device sync
            ):
                event_type = event.get("type", "message")
                data = json.dumps(event)
                yield f"event: {event_type}\ndata: {data}\n\n"

        except Exception as e:
            logger.error(f"Stream error: {e}")
            error_data = json.dumps({"type": "error", "message": str(e)})
            yield f"event: error\ndata: {error_data}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/conversation/start")
async def start_conversation(
    request: ConversationRequest,
    http_request: Request,
    token: str = Depends(require_auth),
    _: None = Depends(require_claude_auth)
):
    """
    Start a conversation that runs in the background.

    Unlike /conversation/stream, this endpoint returns immediately after starting
    the query. The actual streaming happens via WebSocket (/ws/sessions/{session_id}).

    Benefits:
    - Work continues even if the HTTP connection is lost
    - Phone can be locked without stopping the query
    - Browser tab can be backgrounded
    - Use /session/{session_id}/interrupt to stop

    Returns:
        session_id: The ID of the session (new or existing)
        status: "started" if the query was launched successfully
    """
    # Get API user if authenticated via API key
    api_user = get_api_user_from_request(http_request)

    # Determine profile and project
    if api_user:
        profile_id = api_user.get("profile_id") or request.profile or "claude-code"
        project_id = api_user.get("project_id") or request.project
        api_user_id = api_user.get("id")
    else:
        profile_id = request.profile or "claude-code"
        project_id = request.project
        api_user_id = None

    try:
        overrides = None
        if request.overrides:
            overrides = request.overrides.model_dump(exclude_none=True)

        result = await start_background_query(
            prompt=request.prompt,
            profile_id=profile_id,
            project_id=project_id,
            overrides=overrides,
            session_id=request.session_id,
            api_user_id=api_user_id,
            device_id=request.device_id
        )

        return result

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Start conversation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start conversation: {str(e)}"
        )


@router.post("/session/{session_id}/interrupt")
async def interrupt(
    session_id: str,
    token: str = Depends(require_auth)
):
    """
    Interrupt an active streaming session.
    Returns success if the session was interrupted, error if not found or already completed.
    """
    success = await interrupt_session(session_id)

    if success:
        return {"status": "interrupted", "session_id": session_id}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active session found with ID: {session_id}"
        )


@router.get("/sessions/active")
async def list_active_sessions(
    token: str = Depends(require_auth)
):
    """
    List all currently active streaming sessions.
    """
    return {"active_sessions": get_active_sessions()}

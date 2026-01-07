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
from app.core.query_engine import execute_query, stream_query
from app.core.auth import auth_service
from app.api.auth import require_auth

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
            profile_id=request.profile or "code-reader",
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
    token: str = Depends(require_auth),
    _: None = Depends(require_claude_auth)
):
    """
    SSE streaming conversation.
    """
    async def event_generator():
        try:
            overrides = None
            if request.overrides:
                overrides = request.overrides.model_dump(exclude_none=True)

            async for event in stream_query(
                prompt=request.prompt,
                profile_id=request.profile or "code-reader",
                project_id=request.project,
                overrides=overrides,
                session_id=request.session_id
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

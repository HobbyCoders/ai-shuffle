"""Send one message to many chats at once ("Message all active chats").

Each chat gets the message through the composer's own intake
(``submit_http_query``), so it behaves exactly as if the owner had typed it
into that chat: an idle chat starts a turn, a working chat has it steered into
the running turn, a parked chat wakes on it, and a chat owned by another
worker is placed on that worker. One chat refusing never stops the others; the
answer lists what happened to each.
"""
import asyncio
import logging
import uuid
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import require_auth
from app.api.sessions_helpers import require_session

logger = logging.getLogger(__name__)

router = APIRouter()

# More than any real Active lane; keeps one request bounded.
MAX_BROADCAST_CHATS = 100
# Sends run a few at a time so a long list cannot flood the turn intake.
_SEND_CONCURRENCY = 4


class BroadcastRequest(BaseModel):
    session_ids: List[str] = Field(min_length=1, max_length=MAX_BROADCAST_CHATS)
    prompt: str


class BroadcastResult(BaseModel):
    session_id: str
    # sent: admitted. held: kept for a move in progress, runs after it.
    # maybe_sent: another worker may have taken it, its answer was lost.
    # failed: refused, it will not run.
    status: Literal["sent", "held", "maybe_sent", "failed"]
    detail: Optional[str] = None


class BroadcastResponse(BaseModel):
    results: List[BroadcastResult]
    sent: int


def _client_message_id() -> str:
    # The intake's correlation handle shape: "client-" and 32 hex digits.
    return "client-" + uuid.uuid4().hex


async def _send_one(request: Request, session_id: str, prompt: str) -> BroadcastResult:
    from app.api.chat_query import submit_http_query

    try:
        await require_session(request, session_id)
        result = await submit_http_query(
            request, session_id,
            {"prompt": prompt, "client_message_id": _client_message_id()},
            compact_first=False,
        )
    except HTTPException as exc:
        if exc.status_code == 504:
            return BroadcastResult(session_id=session_id, status="maybe_sent", detail=str(exc.detail))
        detail = "Chat not found" if exc.status_code in (403, 404) else str(exc.detail)
        return BroadcastResult(session_id=session_id, status="failed", detail=detail)
    except Exception:
        logger.exception("broadcast send failed session=%s", session_id)
        return BroadcastResult(session_id=session_id, status="failed", detail="Could not send message")
    return BroadcastResult(session_id=session_id, status="held" if result.get("held") else "sent")


@router.post("/broadcast", response_model=BroadcastResponse)
async def broadcast_message(request: Request, body: BroadcastRequest, token: str = Depends(require_auth)):
    """Send ``prompt`` to every chat in ``session_ids``, as a normal composer send.

    Returns one result per distinct chat, in the order given. The request
    succeeds even when some chats refuse; each result says what happened.
    """
    if not body.prompt.strip():
        raise HTTPException(status_code=422, detail="Write a message first.")
    session_ids = list(dict.fromkeys(s for s in body.session_ids if isinstance(s, str) and s))
    gate = asyncio.Semaphore(_SEND_CONCURRENCY)

    async def bounded(session_id: str) -> BroadcastResult:
        async with gate:
            return await _send_one(request, session_id, body.prompt)

    results = await asyncio.gather(*(bounded(sid) for sid in session_ids))
    sent = sum(1 for r in results if r.status in ("sent", "held"))
    logger.info("broadcast chats=%d sent=%d", len(results), sent)
    return BroadcastResponse(results=list(results), sent=sent)

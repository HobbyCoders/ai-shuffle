"""
Query API routes - main AI interface
"""

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from app.core.models import (
    QueryRequest, QueryResponse, ConversationRequest, QueryMetadata
)
from app.core.query_engine import stream_query, interrupt_session, start_background_query
from app.core.session_title import derive_session_title
from app.db import database
from app.db.async_database import run_sync
from app.api.auth import get_api_user_from_request, require_api_key, require_auth
from app.api.sessions_helpers import (
    check_session_access_for_api_user, require_session, resolve_api_user_profile,
)
from app.api.sse_utils import is_client_disconnect

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Query"])


async def _execute_query_with_failover(
    *, prompt, profile_id, project_id=None, overrides=None,
    session_id=None, api_user_id=None,
):
    """Thin delegate to :func:`account_failover.execute_query_with_failover`.

    The retry-loop logic now lives in ``account_failover`` so every non-streaming
    caller (automations, connectors, the ScheduleWakeup builtin) shares the same
    seam. This wrapper preserves the query-route call signature.
    """
    from app.core import account_failover

    return await account_failover.execute_query_with_failover(
        prompt=prompt, profile_id=profile_id, project_id=project_id,
        overrides=overrides, session_id=session_id, api_user_id=api_user_id,
    )


async def _stream_query_with_failover(
    *, prompt, profile_id, project_id=None, overrides=None,
    session_id=None, api_user_id=None, device_id=None,
):
    """Single-pass async-generator wrapper over ``stream_query``.

    Native account failover was retired (OC-1), so there is no transparent retry
    or invisible account switching — the stream is forwarded once, straight
    through, with one piece of buffering:

    * 429 ``api_error`` frames are held back rather than leaked, and flushed just
      before the terminal ``done`` frame. Non-429 ``api_error`` frames pass
      through immediately.
    * On a usage-limit ``done`` the OC-1/OC-4 rate-limited notification is
      surfaced via ``attempt_failover`` before the held 429 and the ``done``
      are flushed.
    """
    from app.core.account_failover import is_usage_limit_error, attempt_failover

    sid = session_id
    held_api_error = None  # most-recent 429 api_error, held for the done decision
    async for event in stream_query(
        prompt=prompt, profile_id=profile_id, project_id=project_id,
        overrides=overrides, session_id=sid, api_user_id=api_user_id,
        device_id=device_id, persist_user_message=True,
    ):
        etype = event.get("type")
        if etype == "init":
            sid = event.get("session_id") or sid
            yield event
            continue
        if etype == "api_error":
            try:
                is_429 = int(event.get("status")) == 429
            except (TypeError, ValueError):
                is_429 = False
            if is_429:
                held_api_error = event  # hold; decided at the done frame
                continue
            yield event  # non-429 errors pass through immediately
            continue
        if etype == "done":
            if is_usage_limit_error(event.get("metadata")):
                sid = event.get("session_id") or sid
                # OC-4: surface the rate-limited notification (native
                # failover is retired, so nothing is retried).
                await attempt_failover(sid, api_user_id)
            if held_api_error is not None:
                yield held_api_error
            yield event
            return
        yield event

    # Stream ended without a done frame → flush any held api_error.
    if held_api_error is not None:
        yield held_api_error
    return


async def require_claude_auth():
    """Dependency: this worker may start agent runs (license plus the active
    Claude Code mode's credential; see ``app.core.credential_gate``)."""
    from app.core import credential_gate

    ready, reason = await credential_gate.agent_credentials_ready_async()
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=reason or "This worker is not set up to start agents.",
        )


@router.post("/query", response_model=QueryResponse)
async def one_shot_query(
    request: QueryRequest,
    api_user: dict = Depends(require_api_key),
    _: None = Depends(require_claude_auth)
):
    """
    One-shot query - stateless, creates a new session each time.
    Best for simple queries that don't need conversation history.
    Requires API key authentication. Uses the API user's configured project;
    the profile must be one of the user's assigned agents, if it has any.
    """
    # Assigned agents restrict the profile; the API user's project wins.
    profile_id = resolve_api_user_profile(api_user, request.profile)
    project_id = api_user.get("project_id") or request.project

    if not profile_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No profile configured. Provide 'profile' in the request."
        )

    try:
        overrides = None
        if request.overrides:
            overrides = request.overrides.model_dump(exclude_none=True)

        result = await _execute_query_with_failover(
            prompt=request.prompt,
            profile_id=profile_id,
            project_id=project_id,
            overrides=overrides,
            api_user_id=api_user.get("id")
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
    except Exception:
        logger.exception("Query execution failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Query execution failed"
        )


@router.post("/query/stream")
async def stream_one_shot_query(
    request: QueryRequest,
    api_user: dict = Depends(require_api_key),
    _: None = Depends(require_claude_auth)
):
    """
    SSE streaming one-shot query.
    Requires API key authentication. Uses the API user's configured project;
    the profile must be one of the user's assigned agents, if it has any.
    """
    # Assigned agents restrict the profile; the API user's project wins.
    profile_id = resolve_api_user_profile(api_user, request.profile)
    project_id = api_user.get("project_id") or request.project

    if not profile_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No profile configured. Provide 'profile' in the request."
        )

    async def event_generator():
        try:
            overrides = None
            if request.overrides:
                overrides = request.overrides.model_dump(exclude_none=True)

            async for event in _stream_query_with_failover(
                prompt=request.prompt,
                profile_id=profile_id,
                project_id=project_id,
                overrides=overrides,
                api_user_id=api_user.get("id")
            ):
                event_type = event.get("type", "message")
                data = json.dumps(event)
                yield f"event: {event_type}\ndata: {data}\n\n"

        except (asyncio.CancelledError, ClientDisconnect, BaseExceptionGroup) as exc:
            # A normal SSE client disconnect must end the stream cleanly, NOT
            # bubble a cancellation out of the generator where it collapses into
            # an anyio ExceptionGroup and surfaces as an unhandled 500 through the
            # BaseHTTPMiddleware stack (issue #627). Anything that is not a pure
            # disconnect/cancellation is re-raised unchanged.
            if not is_client_disconnect(exc):
                raise
            logger.debug("SSE query stream closed (client disconnect)")
            return
        except Exception as e:
            logger.error(f"Stream error: {e}")
            error_detail = str(e) if str(e) else type(e).__name__
            error_data = json.dumps({"type": "error", "message": f"Error: {error_detail}"})
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


async def _check_continued_session(api_user: dict, session_id: str | None) -> None:
    """403 when an API user names an existing session it may not access.

    An id with no row is left to the engine, which refuses it.
    """
    if not session_id:
        return
    session = await run_sync(database.get_session, session_id)
    if session:
        check_session_access_for_api_user(api_user, session)


@router.post("/conversation", response_model=QueryResponse)
async def conversation(
    request: ConversationRequest,
    api_user: dict = Depends(require_api_key),
    _: None = Depends(require_claude_auth)
):
    """
    Multi-turn conversation - maintains context across messages.

    If session_id is provided, continues that session (unknown ids are refused);
    otherwise a session row is created here, up front, before any turn runs.

    The prompt is enrolled as a durable ``source=api`` turn on the v2 turn queue
    rather than executed straight against the query engine. That is what makes a
    message into a BUSY session safe: the one-live-turn-per-session index means
    the new turn queues behind the live one and drains at settlement, instead of
    forking a second agent loop against the same session (a split conversation
    and untracked spend). We then block until the session drains and read the
    outcome and the assistant text back out of the durable event log.

    Requires API key authentication. Uses the API user's configured project;
    the profile must be one of the user's assigned agents, if it has any.
    """
    # Assigned agents restrict the profile; the API user's project wins.
    profile_id = resolve_api_user_profile(api_user, request.profile)
    project_id = api_user.get("project_id") or request.project

    if not profile_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No profile configured. Provide 'profile' in the request."
        )

    try:
        overrides = None
        if request.overrides:
            overrides = request.overrides.model_dump(exclude_none=True)

        from app.core import turn_manager, turn_outcome
        from app.db import database
        from app.db.async_database import run_sync

        # ``submit`` requires an existing session row: resolve the caller's id or
        # mint one here (the old engine created it as a side effect of running).
        # Both DB calls are synchronous psycopg2 — offloaded so they cannot
        # block the event loop (and with it every other request) while this
        # long-running endpoint sets up.
        session_id = request.session_id
        if session_id:
            existing = await run_sync(database.get_session, session_id)
            if existing is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Unknown session_id",
                )
            check_session_access_for_api_user(api_user, existing)
        else:
            session_id = str(uuid.uuid4())
            title = derive_session_title(
                request.prompt, max_chars=50, word_boundary=False
            )
            await run_sync(
                database.create_session,
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                title=title,
                api_user_id=api_user.get("id"),
                is_active=True,
                # The caller's requested model/effort/permission bind the new
                # session (v79) — agents no longer carry one.
                model=(overrides or {}).get("model"),
                effort=(overrides or {}).get("effort"),
                permission_mode=(overrides or {}).get("permission_mode"),
                context_window=(overrides or {}).get("context_window"),
                fast=(overrides or {}).get("fast"),
            )
            # Best-effort: surface the new session on connected devices right
            # away rather than on the next sidebar poll.
            try:
                from app.core.sync_engine import sync_engine

                await sync_engine.broadcast_user_session_created(
                    user_id="admin",
                    session_id=session_id,
                    source_device_id=None,
                )
            except Exception:
                logger.debug("session-created broadcast failed", exc_info=True)

        started = time.monotonic()
        # ``submit_async`` runs the three queue writes in ONE worker-thread hop
        # and fires the scheduler kick back on this loop — same semantics as
        # ``submit``, without blocking every other request on the DB writes.
        await turn_manager.submit_async(
            session_id,
            "api",
            prompt=request.prompt,
            profile_id=profile_id,
            project_id=project_id,
            overrides=overrides,
            # The runtime runs a queued turn as the key on its payload (as the
            # socket and resume paths do); without it the key's turn ran with
            # the owner's credentials, tools and notes.
            payload={"api_user_id": api_user["id"]} if api_user.get("id") else None,
        )

        # Block until the whole queue drains — a hand-off (failover retry,
        # compact auto-continue) is a NEW turn, so waiting for idle covers the
        # successor too. On timeout the turn is left running: it is durable and
        # will settle on its own; only this HTTP call gives up.
        idle = await turn_manager.wait_session_idle(session_id, timeout=600)
        if not idle:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Conversation timed out; the turn may still be running.",
            )

        outcome = turn_outcome.final_turn_outcome(session_id)
        if outcome["error"]:
            logger.error(
                "Conversation turn failed for session %s: %s",
                session_id,
                outcome["error"],
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Conversation failed"
            )

        response_text = turn_outcome.extract_final_assistant_text(session_id)

        usage = outcome["usage"] or {}
        cost = usage.get("total_cost_usd")
        tokens_in = usage.get("tokens_in")
        tokens_out = usage.get("tokens_out")
        metadata = QueryMetadata(
            model=usage.get("model"),
            duration_ms=int((time.monotonic() - started) * 1000),
            total_cost_usd=float(cost) if cost is not None else None,
            tokens_in=int(tokens_in) if tokens_in is not None else None,
            tokens_out=int(tokens_out) if tokens_out is not None else None,
            num_turns=usage.get("num_turns"),
            terminal_reason="interrupted" if outcome["interrupted"] else None,
        )

        return {
            "response": response_text,
            "session_id": session_id,
            "metadata": metadata,
        }

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception:
        logger.exception("Conversation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Conversation failed"
        )


@router.post("/conversation/stream")
async def stream_conversation(
    request: ConversationRequest,
    api_user: dict = Depends(require_api_key),
    _: None = Depends(require_claude_auth)
):
    """
    SSE streaming conversation.
    Requires API key authentication. Uses the API user's configured project;
    the profile must be one of the user's assigned agents, if it has any.
    """
    # Assigned agents restrict the profile; the API user's project wins.
    profile_id = resolve_api_user_profile(api_user, request.profile)
    project_id = api_user.get("project_id") or request.project
    api_user_id = api_user.get("id")

    if not profile_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No profile configured. Provide 'profile' in the request."
        )
    await _check_continued_session(api_user, request.session_id)

    async def event_generator():
        try:
            overrides = None
            if request.overrides:
                overrides = request.overrides.model_dump(exclude_none=True)

            async for event in _stream_query_with_failover(
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

        except (asyncio.CancelledError, ClientDisconnect, BaseExceptionGroup) as exc:
            # A normal SSE client disconnect must end the stream cleanly, NOT
            # bubble a cancellation out of the generator where it collapses into
            # an anyio ExceptionGroup and surfaces as an unhandled 500 through the
            # BaseHTTPMiddleware stack (issue #627). Anything that is not a pure
            # disconnect/cancellation is re-raised unchanged.
            if not is_client_disconnect(exc):
                raise
            logger.debug("SSE conversation stream closed (client disconnect)")
            return
        except Exception as e:
            logger.error(f"Stream error: {e}")
            error_detail = str(e) if str(e) else type(e).__name__
            error_data = json.dumps({"type": "error", "message": f"Error: {error_detail}"})
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
    api_user: dict = Depends(require_api_key),
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

    Requires API key authentication. Uses the API user's configured project;
    the profile must be one of the user's assigned agents, if it has any.

    Returns:
        session_id: The ID of the session (new or existing)
        status: "started" if the query was launched successfully
    """
    # Assigned agents restrict the profile; the API user's project wins.
    profile_id = resolve_api_user_profile(api_user, request.profile)
    project_id = api_user.get("project_id") or request.project
    api_user_id = api_user.get("id")

    if not profile_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No profile configured. Provide 'profile' in the request."
        )
    await _check_continued_session(api_user, request.session_id)

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
    except Exception:
        logger.exception("Failed to start conversation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start conversation"
        )


@router.post("/session/{session_id}/interrupt")
async def interrupt(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth)
):
    """
    Interrupt an active streaming session.
    Returns success if the session was interrupted, error if not found or already completed.
    A restricted API user may only interrupt sessions it can access (403 otherwise).
    """
    if get_api_user_from_request(request):
        await require_session(request, session_id)
    success = await interrupt_session(session_id)

    if success:
        return {"status": "interrupted", "session_id": session_id}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active session found with ID: {session_id}"
        )


@router.get("/streaming/active")
async def list_active_sessions(
    request: Request,
    token: str = Depends(require_auth)
):
    """
    List all currently streaming sessions (actively generating responses).
    Note: This returns sessions that are actively streaming, not just connected.
    An API user only sees the sessions it can access.
    """
    from app.core.query_engine import get_streaming_sessions
    active = get_streaming_sessions()
    api_user = get_api_user_from_request(request)
    if api_user:
        active = await run_sync(_accessible_session_ids, api_user, active)
    return {"active_sessions": active}


def _accessible_session_ids(api_user: dict, session_ids: list) -> list:
    """The subset of ``session_ids`` the API user may access (unknown ids drop)."""
    visible = []
    for session_id in session_ids:
        session = database.get_session(session_id)
        if not session:
            continue
        try:
            check_session_access_for_api_user(api_user, session)
        except HTTPException:
            continue
        visible.append(session_id)
    return visible

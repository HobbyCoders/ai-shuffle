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
    """Dependency: the license lets this worker start agent runs.

    Only the license half of ``app.core.credential_gate``. Whether a run also
    needs the Claude Code credential depends on the leg it runs on: a turn on
    an OpenAI, xAI, OpenRouter or custom provider row needs none. Each route
    checks that with :func:`_refuse_unready_leg` once it knows the profile,
    the overrides and the session.
    """
    from app.core.license import license_allows_agent_runs

    allowed, reason = await asyncio.to_thread(license_allows_agent_runs)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=reason or "This worker is not set up to start agents.",
        )


def _request_overrides(request) -> dict | None:
    if not request.overrides:
        return None
    return request.overrides.model_dump(exclude_none=True)


def _request_leg(profile_id: str, overrides: dict | None, session_id: str | None) -> str | None:
    """The harness leg a request will run on, or None when it cannot tell.

    Mirrors how ``query_options.build_options_from_profile`` binds a run
    without building the options (which can create a git worktree): the
    overrides' provider, else the session's bound provider (a new session is
    bound from the agent's ``config.provider``, as ``create_session`` does);
    the model from the overrides, then the session, then the agent. A
    ``prov:<provider>:<model>`` pick binds that provider, and a plain Claude
    pick the bound provider does not curate drops back to native Claude. The
    leg itself comes from the adapter's own decision table.
    """
    from types import SimpleNamespace

    from app.core.model_registry import DEFAULT_MODEL
    from app.core.provider.harness_adapter import provider_kind_for_sdk_options
    from app.core.query_options import decode_provider_model, is_native_claude_model

    profile = database.get_profile(profile_id)
    if not profile:
        return None  # the engine reports the unknown agent
    config = profile.get("config") or {}
    overrides = overrides or {}
    session = database.get_session(session_id) if session_id else None

    if session is not None:
        provider_id = overrides.get("provider") or session.get("provider_id")
    else:
        provider_id = overrides.get("provider") or config.get("provider")
    selected = overrides.get("model") or (session or {}).get("model")
    model = selected or (session or {}).get("launched_model") or config.get("model")

    decoded = decode_provider_model(selected)
    if decoded is not None:
        provider_id, model = decoded
    elif selected and provider_id and not overrides.get("provider") \
            and is_native_claude_model(selected):
        from app.core.provider_agent import curated_model_ids

        row = database.get_model_provider(provider_id, include_token=False)
        if selected not in curated_model_ids(row):
            provider_id = None
    if not model and not provider_id:
        # No model named: the same default build_options_from_profile uses.
        from app.core.credential_gate import default_run_model

        # An API run is unattended: no most used chat model (query_options
        # gives the REST lane, which carries no typed turn source, the same).
        provider_id, model = default_run_model(prefer_most_used=False)
        model = model or DEFAULT_MODEL
    return provider_kind_for_sdk_options(
        SimpleNamespace(model=model, aih_provider_id=provider_id)
    )


async def _refuse_unready_leg(
    profile_id: str, overrides: dict | None, session_id: str | None = None,
) -> None:
    """503 with the setup message when the run's leg is not set up.

    A run on the Claude Code CLI needs the active Claude Code mode's
    credential; a run on a provider row needs only the license (see
    ``credential_gate.turn_refusal``). The error carries the gate's
    ``agent_setup_required`` code in the ``X-Error-Code`` header. When the leg
    cannot be worked out here, the request goes on: every engine runs the same
    per-leg check on its built options before it starts a turn.
    """
    from app.core import credential_gate

    try:
        leg = await asyncio.to_thread(_request_leg, profile_id, overrides, session_id)
    except Exception:  # noqa: BLE001 — the engine's own gate still applies
        logger.debug("Could not work out the leg for the credential gate", exc_info=True)
        return
    if leg is None:
        return
    reason = await credential_gate.turn_refusal_async(leg)
    if reason:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=reason,
            headers={"X-Error-Code": credential_gate.SETUP_CODE},
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
    overrides = _request_overrides(request)
    await _refuse_unready_leg(profile_id, overrides)

    try:
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
    overrides = _request_overrides(request)
    await _refuse_unready_leg(profile_id, overrides)

    async def event_generator():
        try:
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
        overrides = _request_overrides(request)

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
            from app.core import archive_guard

            if archive_guard.is_archived_row(existing):
                archive_guard.log_dropped(session_id, "api", "conversation refused")
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(archive_guard.SessionArchived(session_id)),
                )
        # The queued turn is refused by the engine too, but only as a failed
        # outcome after the wait; refuse here, before a session row is made.
        await _refuse_unready_leg(profile_id, overrides, session_id)
        if not session_id:
            # The new row's agent foreign key would fail the insert with a
            # 500: refuse a deleted agent here, as /sessions does.
            if not await run_sync(database.get_profile, profile_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="That agent no longer exists — pick another one.",
                )
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
            explicit_permission=True,
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
    overrides = _request_overrides(request)
    await _refuse_unready_leg(profile_id, overrides, request.session_id)

    async def event_generator():
        try:
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
    overrides = _request_overrides(request)
    # The background turn is refused by the engine too, but only after this
    # call has already answered "started"; refuse here so the caller sees it.
    await _refuse_unready_leg(profile_id, overrides, request.session_id)

    try:
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

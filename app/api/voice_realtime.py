"""
Realtime voice — HTTP + WebSocket endpoints.

Endpoints:
    POST   /api/v1/voice/realtime/offer       — SDP offer → answer; starts pipeline
    POST   /api/v1/voice/realtime/{id}/close  — hang up
    GET    /api/v1/voice/realtime/{id}        — fetch session state + transcript
    WS     /api/v1/voice/realtime/{id}/events — live transcripts + metrics feed
    GET    /api/v1/voice/realtime/capabilities — probe: available providers + status

The WebRTC transport is established entirely via a single HTTP round-trip
(SDP offer/answer); no signaling WS is needed. The optional events WS is a
read-only sidechannel for UI updates (captions, metrics).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.api.auth import require_auth
from app.api.ws_auth import authenticate_websocket
from app.db import database
from app.voice import voice_session_manager, VoiceSession
from app.voice.providers import (
    VoiceProviderError,
    load_voice_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/voice/realtime", tags=["Voice"])


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------


class OfferRequest(BaseModel):
    sdp: str
    type: str = "offer"
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    system_prompt: Optional[str] = None


class OfferResponse(BaseModel):
    voice_session_id: str
    sdp: str
    type: str
    stt_provider: str
    tts_provider: str


class SessionStateResponse(BaseModel):
    id: str
    closed: bool
    close_reason: Optional[str]
    transcript: list[dict]
    created_at: float
    last_activity: float


class CapabilitiesResponse(BaseModel):
    stt_providers: list[dict]
    tts_providers: list[dict]
    pipecat_available: bool
    error: Optional[str] = None


# -----------------------------------------------------------------------------
# Capabilities probe — lets the UI show "requires API key" inline
# -----------------------------------------------------------------------------


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(token: str = Depends(require_auth)):
    """Report provider availability and Pipecat install status."""
    cfg = load_voice_config()

    pipecat_available = True
    import_error: Optional[str] = None
    try:
        import pipecat  # noqa: F401
    except ImportError as e:
        pipecat_available = False
        import_error = str(e)

    stt_providers = [
        {
            "id": "deepgram",
            "name": "Deepgram Nova-2",
            "available": bool(_key("deepgram_api_key")),
            "streaming": True,
        },
        {
            "id": "groq",
            "name": "Groq Whisper (batch)",
            "available": bool(_key("groq_api_key")),
            "streaming": False,
        },
    ]
    tts_providers = [
        {
            "id": "cartesia",
            "name": "Cartesia Sonic",
            "available": bool(_key("cartesia_api_key")),
            "streaming": True,
        },
        {
            "id": "elevenlabs",
            "name": "ElevenLabs Turbo v2.5",
            "available": bool(_key("elevenlabs_api_key")),
            "streaming": True,
        },
    ]
    return CapabilitiesResponse(
        stt_providers=stt_providers,
        tts_providers=tts_providers,
        pipecat_available=pipecat_available,
        error=import_error,
    )


def _key(name: str) -> Optional[str]:
    from app.api.settings_crypto import get_decrypted_api_key
    return get_decrypted_api_key(name)


# -----------------------------------------------------------------------------
# SDP offer → answer + pipeline bootstrap
# -----------------------------------------------------------------------------


@router.post("/offer", response_model=OfferResponse)
async def create_offer(
    request: OfferRequest,
    token: str = Depends(require_auth),
) -> OfferResponse:
    """
    Receive a WebRTC SDP offer from the browser, spin up a Pipecat pipeline,
    and return the SDP answer.

    The pipeline runs in the background until the client hangs up (or
    `/close` is called explicitly).
    """
    user_id = _extract_user_id(token)

    try:
        cfg = load_voice_config()
    except VoiceProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Defer heavy imports so the route module is importable even if Pipecat
    # isn't installed (e.g. during type checks or minimal deployments).
    try:
        from app.voice.transport import create_transport
        from app.voice.pipeline import build_pipeline, default_system_prompt
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "Realtime voice is not available on this server. "
                "Install pipecat-ai and restart. Import error: " + str(e)
            ),
        )

    vs_id = voice_session_manager.new_id()
    voice_session = VoiceSession(
        id=vs_id,
        user_id=user_id,
        project_id=request.project_id,
        session_id=request.session_id,
        stt_provider=cfg.stt_provider,
        stt_model=_stt_model_name(cfg.stt_provider),
        tts_provider=cfg.tts_provider,
        tts_model=_tts_model_name(cfg.tts_provider),
        llm_model=cfg.llm_model,
    )

    try:
        handle = await create_transport(request.sdp, request.type, cfg)
    except VoiceProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to create transport: %s", e)
        raise HTTPException(status_code=500, detail=f"Transport failed: {e}")

    voice_session.transport_handle = handle

    system_prompt = request.system_prompt or default_system_prompt()

    def on_transcript(text: str, is_final: bool) -> None:
        if is_final and text.strip():
            voice_session.add_transcript(text, role="user")

    # Phase 2: if use_agent_sdk is enabled, build SessionOptions bound to the
    # requested project's worktree so voice turns can call tools / MCP / etc.
    session_options = None
    if cfg.use_agent_sdk:
        session_options = _build_session_options(
            request=request,
            voice_session=voice_session,
            system_prompt=system_prompt,
            llm_model=cfg.llm_model,
        )

    def tool_observer(phase: str, name: str, payload) -> None:
        """Mirror tool events into the session transcript for UI display."""
        marker = f"[{phase}] {name}"
        voice_session.add_transcript(marker, role="assistant")

    def on_tts_chars(count: int) -> None:
        voice_session.tts_chars += count

    try:
        components = build_pipeline(
            transport=handle.transport,
            cfg=cfg,
            system_prompt=system_prompt,
            on_transcript=on_transcript,
            session_options=session_options,
            tool_observer=tool_observer if cfg.use_agent_sdk else None,
            on_tts_chars=on_tts_chars,
        )
    except VoiceProviderError as e:
        await handle.peer_connection.close()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to build pipeline: %s", e)
        await handle.peer_connection.close()
        raise HTTPException(status_code=500, detail=f"Pipeline build failed: {e}")

    voice_session.components = components

    try:
        await voice_session_manager.register(voice_session)
    except RuntimeError as e:
        await voice_session.close(reason="concurrency_limit")
        raise HTTPException(status_code=429, detail=str(e))

    voice_session.task_handle = asyncio.create_task(voice_session.run())

    # Auto-unregister when the pipeline task finishes.
    def _on_done(_task: asyncio.Task) -> None:
        asyncio.create_task(voice_session_manager.unregister(vs_id))

    voice_session.task_handle.add_done_callback(_on_done)

    return OfferResponse(
        voice_session_id=vs_id,
        sdp=handle.answer_sdp,
        type=handle.answer_type,
        stt_provider=cfg.stt_provider,
        tts_provider=cfg.tts_provider,
    )


@router.post("/{voice_session_id}/close")
async def close_session(
    voice_session_id: str,
    token: str = Depends(require_auth),
):
    session = voice_session_manager.get(voice_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Voice session not found")
    await session.close(reason="user_hangup")
    await voice_session_manager.unregister(voice_session_id)
    return {"success": True}


@router.get("/{voice_session_id}", response_model=SessionStateResponse)
async def get_session(
    voice_session_id: str,
    token: str = Depends(require_auth),
):
    session = voice_session_manager.get(voice_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Voice session not found")
    return SessionStateResponse(
        id=session.id,
        closed=session.closed,
        close_reason=session.close_reason,
        transcript=[
            {"role": t.role, "text": t.text, "timestamp": t.timestamp}
            for t in session.transcript
        ],
        created_at=session.created_at,
        last_activity=session.last_activity,
    )


# -----------------------------------------------------------------------------
# Event feed (WebSocket, read-only sidechannel for the UI)
# -----------------------------------------------------------------------------


@router.websocket("/{voice_session_id}/events")
async def session_events(
    websocket: WebSocket,
    voice_session_id: str,
    token: Optional[str] = None,
):
    """
    Emit periodic state snapshots + transcript deltas to the client.
    Client authenticates via cookie or query-param token (same pattern as /ws/chat).
    """
    await websocket.accept()

    is_authenticated, _api_user = await authenticate_websocket(websocket, token)
    if not is_authenticated:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    session = voice_session_manager.get(voice_session_id)
    if session is None:
        await websocket.close(code=4404, reason="Voice session not found")
        return

    last_transcript_len = 0
    try:
        while not session.closed:
            # Push any new transcript lines.
            if len(session.transcript) > last_transcript_len:
                new = session.transcript[last_transcript_len:]
                last_transcript_len = len(session.transcript)
                for entry in new:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "transcript",
                                "role": entry.role,
                                "text": entry.text,
                                "timestamp": entry.timestamp,
                            }
                        )
                    )

            await websocket.send_text(
                json.dumps(
                    {
                        "type": "state",
                        "closed": session.closed,
                        "last_activity": session.last_activity,
                    }
                )
            )
            await asyncio.sleep(0.5)

        await websocket.send_text(
            json.dumps(
                {"type": "closed", "reason": session.close_reason or "ended"}
            )
        )
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.debug("Voice events WS error: %s", e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _stt_model_name(provider: str) -> str:
    """Map provider → default model ID used by build_stt_service."""
    return {
        "deepgram": "nova-2-general",
        "groq": "whisper-large-v3-turbo",
    }.get(provider, provider)


def _tts_model_name(provider: str) -> str:
    return {
        "cartesia": "sonic-english",
        "elevenlabs": "turbo-v2_5",
    }.get(provider, provider)


def _extract_user_id(token_payload) -> Optional[str]:
    """Best-effort user id from auth dependency payload (may be dict or str)."""
    if isinstance(token_payload, dict):
        return token_payload.get("user_id") or token_payload.get("sub")
    return None


def _build_session_options(
    request: OfferRequest,
    voice_session: VoiceSession,
    system_prompt: str,
    llm_model: str,
):
    """
    Build a SDK SessionOptions for Phase 2 voice-with-tools.

    Resolves cwd via the project's worktree path (preferred over project path
    per CLAUDE.md worktree discipline). Restricts tools to a safe default when
    no user-confirmed permission rules are present.
    """
    from app.core.provider.types import SessionOptions
    from app.core.project_utils import get_project_working_dir

    cwd: Optional[str] = None
    if request.project_id:
        project = database.get_project(request.project_id)
        if project:
            worktree_path = project.get("worktree_path") if isinstance(project, dict) else None
            cwd = worktree_path or get_project_working_dir(project)
            voice_session.worktree_path = cwd

    return SessionOptions(
        model=llm_model,
        system_prompt=system_prompt,
        cwd=cwd,
        resume_session_id=request.session_id,
        # Conservative voice default: read-only safe tools unless rules say otherwise.
        # Phase 3 surfaces per-profile control in the UI.
        allowed_tools=["Read", "Grep", "Glob", "WebFetch", "WebSearch"],
        permission_mode="default",
        include_partial_messages=True,
    )

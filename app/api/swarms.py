"""Owner-authorized workflow snapshot and cancellation endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request
from app.api.auth import require_auth, get_api_user_from_request
from app.api.sessions_helpers import check_transcript_access, require_session
from app.core import swarm

router = APIRouter()

async def owned_session(request: Request, session_id: str) -> dict:
    # Preserve require_auth/require_session controller semantics: paired owner
    # devices have no API-user identity, just like the install owner/admin.
    # API-key callers additionally must own the session, not merely its project.
    session = await require_session(request, session_id)
    user = get_api_user_from_request(request)
    if user and str(session.get("api_user_id")) != str(user.get("id")):
        raise HTTPException(status_code=403, detail="Access denied to this session")
    return session

@router.get("/{session_id}/swarms")
async def list_swarms(session_id: str, request: Request, token: str = Depends(require_auth)):
    # Snapshots carry task briefs and reports: the transcript rule (#1877).
    await check_transcript_access(request, await owned_session(request, session_id))
    return {"swarms": await swarm.snapshots(session_id)}

@router.post("/{session_id}/swarms/{swarm_id}/cancel")
async def cancel_swarm(session_id: str, swarm_id: str, request: Request, token: str = Depends(require_auth)):
    await owned_session(request, session_id)
    try:
        return {"swarm": await swarm.cancel(session_id, swarm_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except swarm.SwarmUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

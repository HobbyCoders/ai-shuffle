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
    session = await owned_session(request, session_id)
    # A chat that runs on another worker: its coordinator is there.
    from app.api.task_stop_owner import forward_swarm_cancel
    forwarded = await forward_swarm_cancel(session, swarm_id)
    if forwarded is not None:
        if "swarm" not in forwarded:
            raise HTTPException(status_code=404 if forwarded["error"] == "unknown" else 409,
                                detail=forwarded["detail"])
        snapshot = forwarded["swarm"]
        if isinstance(snapshot, dict) and "tasks" not in snapshot:
            # A receipt (the link was not HTTPS): the tasks come from our
            # synced copy, which catches up with the cancel as events arrive.
            synced = [s for s in await swarm.snapshots(session_id) if s.get("id") == swarm_id]
            snapshot = synced[0] if synced else snapshot
    else:
        try:
            snapshot = await swarm.cancel(session_id, swarm_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except swarm.SwarmUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"swarm": await _readable(request, session, snapshot)}


async def _readable(request: Request, session: dict, snapshot):
    """The snapshot carries task briefs and reports: the transcript rule, as
    for GET (#1877). A caller allowed to cancel but not to read gets a receipt."""
    try:
        await check_transcript_access(request, session)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        from app.api.task_stop_owner import swarm_receipt
        return swarm_receipt(snapshot)
    return snapshot

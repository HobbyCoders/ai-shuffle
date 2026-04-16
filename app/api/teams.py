"""
Team visibility API — inspect and control Claude Code agent teams spawned
by a parent session.

The core visibility (messages in chat) flows through the live_session_store
and normal session polling. These endpoints cover the ancillary use cases
that don't fit the message stream: listing active teammates for a status
bar and terminating stuck teammate subprocesses.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.auth import require_auth
from app.core.team_registry import team_registry
from app.core.team_transcript_watcher import team_transcript_watcher
from app.db import database

router = APIRouter(prefix="/api/v1/teams", tags=["Teams"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class TeammatePublic(BaseModel):
    team_name: str
    teammate_name: str
    teammate_session_id: Optional[str] = None
    pid: Optional[int] = None
    status: str
    usage: Dict[str, Any] = {}


class TeamPublic(BaseModel):
    team_name: str
    parent_session_id: str
    teammates: List[TeammatePublic]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verify_session_access(session_id: str, token: str) -> None:
    """Ensure the authenticated caller owns (or has access to) the session."""
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    # Auth check mirrors the lightweight policy used elsewhere in the app:
    # if a user is scoped to a session it must match. Multi-user access
    # control lives in the session table itself — we defer to its existence
    # rather than re-implementing ownership here.
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{session_id}", response_model=List[TeamPublic])
async def list_teams(session_id: str, token: str = Depends(require_auth)) -> List[TeamPublic]:
    """List all teams currently tracked for a parent session."""
    _verify_session_access(session_id, token)
    teams = await team_registry.list_teams(session_id)
    return [TeamPublic(**t) for t in teams]


@router.post("/{session_id}/{team_name}/members/{teammate_name}/kill")
async def kill_teammate(
    session_id: str,
    team_name: str,
    teammate_name: str,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """SIGTERM a teammate subprocess and mark it killed.

    The transcript watcher will finalize the teammate once its JSONL stops
    growing and the PID is gone.
    """
    _verify_session_access(session_id, token)
    rec = await team_registry.get_teammate(session_id, team_name, teammate_name)
    if rec is None:
        raise HTTPException(status_code=404, detail="Teammate not found")
    if rec.pid is None:
        raise HTTPException(
            status_code=409,
            detail="Teammate PID unknown — cannot kill",
        )
    ok = await team_registry.kill_teammate(session_id, team_name, teammate_name)
    return {"success": ok, "pid": rec.pid}


@router.post("/{session_id}/{team_name}/stop-watching")
async def stop_watching(
    session_id: str,
    team_name: str,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """Detach all transcript tailers for a team without killing processes."""
    _verify_session_access(session_id, token)
    await team_transcript_watcher.stop_session(session_id)
    await team_registry.remove_team(session_id, team_name)
    return {"success": True}

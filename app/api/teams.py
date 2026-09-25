"""
Team visibility API — inspect and control Claude Code agent teams spawned
by a parent session.

The core visibility (messages in chat) flows through the session event feed
and normal session polling. These endpoints cover the ancillary use cases
that don't fit the message stream: listing active teammates for a status
bar and terminating stuck teammate subprocesses.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.auth import require_admin
from app.core.team_registry import team_registry
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
    """404 for an unknown session. Both routes are owner-only
    (``require_admin``): the team registry records no per-key ownership, so
    an API key never reaches another principal's teammates through them."""
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{session_id}", response_model=List[TeamPublic])
async def list_teams(session_id: str, token: str = Depends(require_admin)) -> List[TeamPublic]:
    """List all teams currently tracked for a parent session."""
    _verify_session_access(session_id, token)
    teams = await team_registry.list_teams(session_id)
    return [TeamPublic(**t) for t in teams]


@router.post("/{session_id}/{team_name}/members/{teammate_name}/kill")
async def kill_teammate(
    session_id: str,
    team_name: str,
    teammate_name: str,
    token: str = Depends(require_admin),
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

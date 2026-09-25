"""
Team visibility API — inspect and control Claude Code agent teams spawned
by a parent session.

The core visibility (messages in chat) flows through the session event feed
and normal session polling. These endpoints cover the ancillary use cases
that don't fit the message stream: listing active teammates for a status
bar and terminating stuck teammate subprocesses.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.auth import require_auth
from app.api.sessions_helpers import require_session
from app.core.team_registry import team_registry

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


# Access: the team registry is keyed by the parent session, and a session's
# teammates are that session's. So both routes take the session access rule
# (``require_session``): 404 for an unknown session, the owner reaches every
# session, and an API key only the sessions it owns (inside its project and
# agent scope), so a key can see and stop its own sessions' teammates but
# never another principal's.


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{session_id}", response_model=List[TeamPublic])
async def list_teams(
    session_id: str, request: Request, token: str = Depends(require_auth),
) -> List[TeamPublic]:
    """List all teams currently tracked for a parent session."""
    await require_session(request, session_id)
    teams = await team_registry.list_teams(session_id)
    return [TeamPublic(**t) for t in teams]


@router.post("/{session_id}/{team_name}/members/{teammate_name}/kill")
async def kill_teammate(
    session_id: str,
    team_name: str,
    teammate_name: str,
    request: Request,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    """SIGTERM a teammate subprocess and mark it killed.

    The transcript watcher will finalize the teammate once its JSONL stops
    growing and the PID is gone.
    """
    await require_session(request, session_id)
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

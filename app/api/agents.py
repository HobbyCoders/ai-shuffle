"""
Background Agent Management API routes.

This module provides endpoints for launching, monitoring, and controlling
background AI agents that run tasks autonomously.
"""

import asyncio
import logging
from typing import Optional, List, Dict, Any, Set
from datetime import datetime
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Query, status
from fastapi.websockets import WebSocketState
from pydantic import BaseModel, Field

from app.api.auth import require_auth, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["Agents"])

# In-memory storage for MVP
agents_db: Dict[str, Dict[str, Any]] = {}
agent_logs_db: Dict[str, List[Dict[str, Any]]] = {}

# WebSocket connections for real-time updates
_agent_websockets: Set[WebSocket] = set()


# ============================================================================
# Request/Response Models
# ============================================================================

class AgentTask(BaseModel):
    """Individual task within an agent's execution"""
    id: str
    name: str
    status: str  # pending, in_progress, completed, failed
    children: Optional[List['AgentTask']] = None


class AgentLaunchRequest(BaseModel):
    """Request to launch a new background agent"""
    name: str = Field(..., min_length=1, max_length=100, description="Agent name/title")
    prompt: str = Field(..., min_length=1, description="The task prompt for the agent")
    profile_id: Optional[str] = Field(None, description="Profile ID to use")
    project_id: Optional[str] = Field(None, description="Project ID to work in")
    auto_branch: bool = Field(True, description="Automatically create a git branch")
    auto_pr: bool = Field(False, description="Automatically create a pull request on completion")
    max_duration_minutes: int = Field(30, ge=1, le=480, description="Maximum run duration in minutes")


class AgentResponse(BaseModel):
    """Agent status response"""
    id: str
    name: str
    prompt: str
    status: str  # queued, running, paused, completed, failed
    progress: float = Field(..., ge=0, le=100)
    branch: Optional[str] = None
    tasks: List[AgentTask] = []
    started_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    profile_id: Optional[str] = None
    project_id: Optional[str] = None
    auto_branch: bool = True
    auto_pr: bool = False
    max_duration_minutes: int = 30


class AgentLogEntry(BaseModel):
    """Log entry for an agent"""
    timestamp: datetime
    level: str  # info, warning, error, debug
    message: str
    metadata: Optional[Dict[str, Any]] = None


class AgentLogsResponse(BaseModel):
    """Paginated logs response"""
    agent_id: str
    logs: List[AgentLogEntry]
    total: int
    offset: int
    limit: int


class AgentListResponse(BaseModel):
    """List of agents response"""
    agents: List[AgentResponse]
    total: int


# ============================================================================
# Helper Functions
# ============================================================================

def _create_agent_response(agent_data: Dict[str, Any]) -> AgentResponse:
    """Convert internal agent data to response model"""
    return AgentResponse(
        id=agent_data["id"],
        name=agent_data["name"],
        prompt=agent_data["prompt"],
        status=agent_data["status"],
        progress=agent_data.get("progress", 0.0),
        branch=agent_data.get("branch"),
        tasks=agent_data.get("tasks", []),
        started_at=agent_data["started_at"],
        completed_at=agent_data.get("completed_at"),
        error=agent_data.get("error"),
        profile_id=agent_data.get("profile_id"),
        project_id=agent_data.get("project_id"),
        auto_branch=agent_data.get("auto_branch", True),
        auto_pr=agent_data.get("auto_pr", False),
        max_duration_minutes=agent_data.get("max_duration_minutes", 30)
    )


def _add_log(agent_id: str, level: str, message: str, metadata: Optional[Dict[str, Any]] = None):
    """Add a log entry for an agent"""
    if agent_id not in agent_logs_db:
        agent_logs_db[agent_id] = []

    agent_logs_db[agent_id].append({
        "timestamp": datetime.utcnow(),
        "level": level,
        "message": message,
        "metadata": metadata
    })


async def _broadcast_agent_update(agent_id: str, event_type: str, data: Dict[str, Any]):
    """Broadcast agent update to all connected WebSocket clients"""
    message = {
        "type": event_type,
        "agent_id": agent_id,
        "data": data,
        "timestamp": datetime.utcnow().isoformat()
    }

    disconnected = set()
    for ws in _agent_websockets:
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to send WebSocket message: {e}")
                disconnected.add(ws)
        else:
            disconnected.add(ws)

    # Clean up disconnected sockets
    _agent_websockets.difference_update(disconnected)


# ============================================================================
# Agent CRUD Endpoints
# ============================================================================

@router.post("/launch", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def launch_agent(request: AgentLaunchRequest, token: str = require_auth):
    """
    Launch a new background agent.

    The agent will start executing the given prompt autonomously.
    Use the WebSocket endpoint or polling to monitor progress.
    """
    agent_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # Generate branch name if auto_branch is enabled
    branch_name = None
    if request.auto_branch:
        # Create a safe branch name from agent name
        safe_name = request.name.lower().replace(" ", "-")[:30]
        branch_name = f"agent/{safe_name}-{agent_id[:8]}"

    agent_data = {
        "id": agent_id,
        "name": request.name,
        "prompt": request.prompt,
        "status": "queued",
        "progress": 0.0,
        "branch": branch_name,
        "tasks": [],
        "started_at": now,
        "completed_at": None,
        "error": None,
        "profile_id": request.profile_id,
        "project_id": request.project_id,
        "auto_branch": request.auto_branch,
        "auto_pr": request.auto_pr,
        "max_duration_minutes": request.max_duration_minutes
    }

    agents_db[agent_id] = agent_data
    _add_log(agent_id, "info", f"Agent '{request.name}' created and queued")

    # Broadcast update
    await _broadcast_agent_update(agent_id, "agent_launched", agent_data)

    logger.info(f"Launched agent {agent_id}: {request.name}")

    return _create_agent_response(agent_data)


@router.get("", response_model=AgentListResponse)
async def list_agents(
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = require_auth
):
    """
    List all agents, optionally filtered by status.

    Status values: queued, running, paused, completed, failed
    """
    agents = list(agents_db.values())

    # Filter by status if provided
    if status_filter:
        agents = [a for a in agents if a["status"] == status_filter]

    # Sort by started_at descending (newest first)
    agents.sort(key=lambda x: x["started_at"], reverse=True)

    total = len(agents)

    # Apply pagination
    agents = agents[offset:offset + limit]

    return AgentListResponse(
        agents=[_create_agent_response(a) for a in agents],
        total=total
    )


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str, token: str = require_auth):
    """Get details of a specific agent"""
    if agent_id not in agents_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    return _create_agent_response(agents_db[agent_id])


@router.get("/{agent_id}/logs", response_model=AgentLogsResponse)
async def get_agent_logs(
    agent_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    level: Optional[str] = Query(None, description="Filter by log level"),
    token: str = require_auth
):
    """
    Get logs for an agent with pagination.

    Log levels: debug, info, warning, error
    """
    if agent_id not in agents_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    logs = agent_logs_db.get(agent_id, [])

    # Filter by level if provided
    if level:
        logs = [log for log in logs if log["level"] == level]

    total = len(logs)

    # Apply pagination (logs are in chronological order, return newest first)
    logs = list(reversed(logs))[offset:offset + limit]

    return AgentLogsResponse(
        agent_id=agent_id,
        logs=[
            AgentLogEntry(
                timestamp=log["timestamp"],
                level=log["level"],
                message=log["message"],
                metadata=log.get("metadata")
            )
            for log in logs
        ],
        total=total,
        offset=offset,
        limit=limit
    )


# ============================================================================
# Agent Control Endpoints
# ============================================================================

@router.post("/{agent_id}/pause")
async def pause_agent(agent_id: str, token: str = require_auth):
    """Pause a running agent"""
    if agent_id not in agents_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    agent = agents_db[agent_id]

    if agent["status"] != "running":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot pause agent in '{agent['status']}' status"
        )

    agent["status"] = "paused"
    _add_log(agent_id, "info", "Agent paused")

    await _broadcast_agent_update(agent_id, "agent_paused", {"status": "paused"})

    return {"status": "ok", "message": "Agent paused"}


@router.post("/{agent_id}/resume")
async def resume_agent(agent_id: str, token: str = require_auth):
    """Resume a paused agent"""
    if agent_id not in agents_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    agent = agents_db[agent_id]

    if agent["status"] != "paused":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot resume agent in '{agent['status']}' status"
        )

    agent["status"] = "running"
    _add_log(agent_id, "info", "Agent resumed")

    await _broadcast_agent_update(agent_id, "agent_resumed", {"status": "running"})

    return {"status": "ok", "message": "Agent resumed"}


@router.post("/{agent_id}/cancel")
async def cancel_agent(agent_id: str, token: str = require_auth):
    """Cancel a queued or running agent"""
    if agent_id not in agents_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    agent = agents_db[agent_id]

    if agent["status"] in ("completed", "failed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel agent in '{agent['status']}' status"
        )

    agent["status"] = "failed"
    agent["error"] = "Cancelled by user"
    agent["completed_at"] = datetime.utcnow()
    _add_log(agent_id, "warning", "Agent cancelled by user")

    await _broadcast_agent_update(agent_id, "agent_cancelled", {
        "status": "failed",
        "error": "Cancelled by user"
    })

    return {"status": "ok", "message": "Agent cancelled"}


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: str, token: str = require_auth):
    """
    Delete an agent record.

    Only completed, failed, or cancelled agents can be deleted.
    """
    if agent_id not in agents_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    agent = agents_db[agent_id]

    if agent["status"] in ("queued", "running", "paused"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete agent in '{agent['status']}' status. Cancel it first."
        )

    del agents_db[agent_id]
    if agent_id in agent_logs_db:
        del agent_logs_db[agent_id]

    await _broadcast_agent_update(agent_id, "agent_deleted", {})

    logger.info(f"Deleted agent {agent_id}")


# ============================================================================
# WebSocket Endpoint
# ============================================================================

@router.websocket("/ws")
async def agents_websocket(
    websocket: WebSocket,
    token: Optional[str] = Query(None, description="Authentication token")
):
    """
    WebSocket endpoint for real-time agent updates.

    Connect to receive updates for all agents. Messages from server:
    - agent_launched: New agent was launched
    - agent_progress: Agent progress updated
    - agent_task_update: Task status changed
    - agent_paused: Agent was paused
    - agent_resumed: Agent was resumed
    - agent_completed: Agent finished successfully
    - agent_failed: Agent encountered an error
    - agent_cancelled: Agent was cancelled
    - agent_deleted: Agent record was deleted
    - ping: Keep-alive message

    Messages to server:
    - pong: Response to ping
    - subscribe: Subscribe to specific agent updates {agent_id}
    - unsubscribe: Unsubscribe from specific agent {agent_id}
    """
    await websocket.accept()

    # Simple auth check using cookie or query param
    from app.api.websocket import authenticate_websocket
    is_authenticated, _ = await authenticate_websocket(websocket, token)

    if not is_authenticated:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    logger.info("Agent WebSocket connected")
    _agent_websockets.add(websocket)

    try:
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=60.0
                )

                msg_type = data.get("type")

                if msg_type == "pong":
                    pass  # Keep-alive response

                elif msg_type == "subscribe":
                    # Could implement per-agent subscriptions here
                    agent_id = data.get("agent_id")
                    if agent_id and agent_id in agents_db:
                        await websocket.send_json({
                            "type": "subscribed",
                            "agent_id": agent_id,
                            "data": agents_db[agent_id]
                        })

                elif msg_type == "unsubscribe":
                    agent_id = data.get("agent_id")
                    await websocket.send_json({
                        "type": "unsubscribed",
                        "agent_id": agent_id
                    })

            except asyncio.TimeoutError:
                if websocket.client_state != WebSocketState.CONNECTED:
                    break
                # Send ping
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break

    except WebSocketDisconnect:
        logger.info("Agent WebSocket disconnected")

    except Exception as e:
        logger.error(f"Agent WebSocket error: {e}")

    finally:
        _agent_websockets.discard(websocket)

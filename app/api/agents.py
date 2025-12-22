"""
Background Agent Management API routes.

This module provides endpoints for launching, monitoring, and controlling
background AI agents that run tasks autonomously.
"""

import asyncio
import logging
from typing import Optional, List, Dict, Any, Set
from datetime import datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Query, status, Depends
from fastapi.websockets import WebSocketState
from pydantic import BaseModel, Field

from app.api.auth import require_auth, require_admin
from app.db import database
from app.core.agent_engine import agent_engine, AgentStatus, TaskStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["Agents"])

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
    auto_review: bool = Field(False, description="Automatically review the PR after creation")
    max_duration_minutes: int = Field(30, ge=1, le=480, description="Maximum run duration in minutes")


class AgentResponse(BaseModel):
    """Agent status response"""
    id: str
    name: str
    prompt: str
    status: str  # queued, running, paused, completed, failed
    progress: float = Field(..., ge=0, le=100)
    branch: Optional[str] = None
    pr_url: Optional[str] = None
    tasks: List[AgentTask] = []
    started_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    result_summary: Optional[str] = None
    profile_id: Optional[str] = None
    project_id: Optional[str] = None
    worktree_id: Optional[str] = None
    auto_branch: bool = True
    auto_pr: bool = False
    auto_review: bool = False
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


class AgentStatsResponse(BaseModel):
    """Agent statistics response"""
    total: int
    running: int
    queued: int
    completed: int
    failed: int
    success_rate: float
    avg_duration_minutes: float
    by_day: Dict[str, int]


# ============================================================================
# Helper Functions
# ============================================================================

def _task_to_model(task: Dict[str, Any]) -> AgentTask:
    """Convert database task to response model"""
    children = task.get("children", [])
    return AgentTask(
        id=task["id"],
        name=task["name"],
        status=task["status"],
        children=[_task_to_model(c) for c in children] if children else None
    )


def _create_agent_response(agent_data: Dict[str, Any]) -> AgentResponse:
    """Convert internal agent data to response model"""
    tasks_tree = database.get_agent_tasks_tree(agent_data["id"])

    return AgentResponse(
        id=agent_data["id"],
        name=agent_data["name"],
        prompt=agent_data["prompt"],
        status=agent_data["status"],
        progress=agent_data.get("progress", 0.0),
        branch=agent_data.get("branch"),
        pr_url=agent_data.get("pr_url"),
        tasks=[_task_to_model(t) for t in tasks_tree],
        started_at=agent_data["started_at"],
        completed_at=agent_data.get("completed_at"),
        error=agent_data.get("error"),
        result_summary=agent_data.get("result_summary"),
        profile_id=agent_data.get("profile_id"),
        project_id=agent_data.get("project_id"),
        worktree_id=agent_data.get("worktree_id"),
        auto_branch=agent_data.get("auto_branch", True),
        auto_pr=agent_data.get("auto_pr", False),
        auto_review=agent_data.get("auto_review", False),
        max_duration_minutes=agent_data.get("max_duration_minutes", 30)
    )


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


# Register broadcast callback with engine
agent_engine.set_broadcast_callback(_broadcast_agent_update)


# ============================================================================
# Agent CRUD Endpoints
# ============================================================================

@router.post("/launch", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def launch_agent(request: AgentLaunchRequest, token: str = Depends(require_auth)):
    """
    Launch a new background agent.

    The agent will start executing the given prompt autonomously.
    Use the WebSocket endpoint or polling to monitor progress.
    """
    agent_run = await agent_engine.launch_agent(
        name=request.name,
        prompt=request.prompt,
        profile_id=request.profile_id,
        project_id=request.project_id,
        auto_branch=request.auto_branch,
        auto_pr=request.auto_pr,
        auto_review=request.auto_review,
        max_duration_minutes=request.max_duration_minutes
    )

    logger.info(f"Launched agent {agent_run['id']}: {request.name}")

    return _create_agent_response(agent_run)


@router.get("", response_model=AgentListResponse)
async def list_agents(
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    project_id: Optional[str] = Query(None, description="Filter by project"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """
    List all agents, optionally filtered by status.

    Status values: queued, running, paused, completed, failed
    """
    agents = database.get_agent_runs(
        status=status_filter,
        project_id=project_id,
        limit=limit,
        offset=offset
    )

    total = database.get_agent_runs_count(status=status_filter, project_id=project_id)

    return AgentListResponse(
        agents=[_create_agent_response(a) for a in agents],
        total=total
    )


@router.get("/stats", response_model=AgentStatsResponse)
async def get_stats(
    days: int = Query(7, ge=1, le=365, description="Number of days to include"),
    project_id: Optional[str] = Query(None, description="Filter by project"),
    token: str = Depends(require_auth)
):
    """Get agent run statistics"""
    stats = database.get_agent_run_stats(days=days, project_id=project_id)
    return AgentStatsResponse(**stats)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str, token: str = Depends(require_auth)):
    """Get details of a specific agent"""
    agent_run = database.get_agent_run(agent_id)
    if not agent_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    return _create_agent_response(agent_run)


@router.get("/{agent_id}/logs", response_model=AgentLogsResponse)
async def get_agent_logs(
    agent_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    level: Optional[str] = Query(None, description="Filter by log level"),
    token: str = Depends(require_auth)
):
    """
    Get logs for an agent with pagination.

    Log levels: debug, info, warning, error
    """
    agent_run = database.get_agent_run(agent_id)
    if not agent_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    logs = database.get_agent_logs(agent_id, level=level, limit=limit, offset=offset)
    total = database.get_agent_logs_count(agent_id, level=level)

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
async def pause_agent(agent_id: str, token: str = Depends(require_auth)):
    """Pause a running agent"""
    agent_run = database.get_agent_run(agent_id)
    if not agent_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    if agent_run["status"] != AgentStatus.RUNNING.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot pause agent in '{agent_run['status']}' status"
        )

    success = await agent_engine.pause_agent(agent_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to pause agent"
        )

    return {"status": "ok", "message": "Agent paused"}


@router.post("/{agent_id}/resume")
async def resume_agent(agent_id: str, token: str = Depends(require_auth)):
    """Resume a paused agent"""
    agent_run = database.get_agent_run(agent_id)
    if not agent_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    if agent_run["status"] != AgentStatus.PAUSED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot resume agent in '{agent_run['status']}' status"
        )

    success = await agent_engine.resume_agent(agent_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to resume agent"
        )

    return {"status": "ok", "message": "Agent resumed"}


@router.post("/{agent_id}/cancel")
async def cancel_agent(agent_id: str, token: str = Depends(require_auth)):
    """Cancel a queued or running agent"""
    agent_run = database.get_agent_run(agent_id)
    if not agent_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    if agent_run["status"] in (AgentStatus.COMPLETED.value, AgentStatus.FAILED.value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel agent in '{agent_run['status']}' status"
        )

    success = await agent_engine.cancel_agent(agent_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to cancel agent"
        )

    return {"status": "ok", "message": "Agent cancelled"}


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: str, token: str = Depends(require_auth)):
    """
    Delete an agent record.

    Only completed, failed, or cancelled agents can be deleted.
    """
    agent_run = database.get_agent_run(agent_id)
    if not agent_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    if agent_run["status"] in (AgentStatus.QUEUED.value, AgentStatus.RUNNING.value, AgentStatus.PAUSED.value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete agent in '{agent_run['status']}' status. Cancel it first."
        )

    database.delete_agent_run(agent_id)
    await _broadcast_agent_update(agent_id, "agent_deleted", {})

    logger.info(f"Deleted agent {agent_id}")


# ============================================================================
# Bulk Operations
# ============================================================================

@router.post("/clear-completed")
async def clear_completed_agents(token: str = Depends(require_auth)):
    """Delete all completed agents"""
    completed = database.get_agent_runs(status=AgentStatus.COMPLETED.value, limit=1000)
    count = 0
    for agent in completed:
        database.delete_agent_run(agent["id"])
        count += 1

    logger.info(f"Cleared {count} completed agents")
    return {"status": "ok", "deleted": count}


@router.post("/clear-failed")
async def clear_failed_agents(token: str = Depends(require_auth)):
    """Delete all failed agents"""
    failed = database.get_agent_runs(status=AgentStatus.FAILED.value, limit=1000)
    count = 0
    for agent in failed:
        database.delete_agent_run(agent["id"])
        count += 1

    logger.info(f"Cleared {count} failed agents")
    return {"status": "ok", "deleted": count}


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
    - agent_started: Agent execution started
    - agent_progress: Agent progress updated
    - agent_task_update: Task status changed
    - agent_log: New log entry
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
                    if agent_id:
                        agent_run = database.get_agent_run(agent_id)
                        if agent_run:
                            await websocket.send_json({
                                "type": "subscribed",
                                "agent_id": agent_id,
                                "data": agent_run
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


# ============================================================================
# Engine Lifecycle
# ============================================================================

async def start_agent_engine():
    """Start the agent execution engine (called from app startup)"""
    await agent_engine.start()
    logger.info("Agent execution engine started")


async def stop_agent_engine():
    """Stop the agent execution engine (called from app shutdown)"""
    await agent_engine.stop()
    logger.info("Agent execution engine stopped")

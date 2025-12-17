"""
Background Agents API routes

Provides endpoints for launching, managing, and monitoring background agents
that run autonomously to complete tasks.
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException, Request, Query, status
from fastapi.websockets import WebSocketState
from pydantic import BaseModel, Field

from app.api.auth import require_auth, require_admin, get_api_user_from_request
from app.db import database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["Agents"])


# ============================================================================
# Pydantic Models
# ============================================================================

class AgentTask(BaseModel):
    """A task within an agent's execution"""
    id: str
    name: str
    status: str = "pending"  # pending, in_progress, completed, failed
    children: List['AgentTask'] = []
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class AgentLaunchRequest(BaseModel):
    """Request to launch a new background agent"""
    name: str = Field(..., description="Name for this agent run")
    prompt: str = Field(..., description="Initial prompt/task for the agent")
    profile_id: Optional[str] = Field(None, description="Profile ID to use (default: claude-code)")
    project_id: Optional[str] = Field(None, description="Project to work in")
    auto_branch: bool = Field(False, description="Automatically create a git branch")
    branch_name: Optional[str] = Field(None, description="Custom branch name (if auto_branch)")
    auto_pr: bool = Field(False, description="Automatically create a PR when done")
    max_duration_hours: Optional[int] = Field(None, description="Maximum runtime in hours")


class AgentResponse(BaseModel):
    """Full agent response with all details"""
    id: str
    name: str
    prompt: str
    status: str  # queued, running, paused, completed, failed, cancelled
    progress: float = 0.0  # 0.0 - 1.0
    profile_id: str
    project_id: Optional[str] = None
    branch: Optional[str] = None
    tasks: List[AgentTask] = []
    logs: List[str] = []
    session_id: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    created_at: datetime
    updated_at: datetime


class AgentListResponse(BaseModel):
    """Agent summary for list view"""
    id: str
    name: str
    status: str
    progress: float
    profile_id: str
    project_id: Optional[str] = None
    branch: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    created_at: datetime


class AgentLogsResponse(BaseModel):
    """Response for agent logs"""
    id: str
    logs: List[str]
    tasks: List[AgentTask]


# ============================================================================
# In-Memory Agent Storage (for MVP - will be persisted to DB later)
# ============================================================================

# In-memory storage for agents
_agents: Dict[str, Dict[str, Any]] = {}
_agent_tasks: Dict[str, asyncio.Task] = {}
_agent_websockets: Dict[str, List[WebSocket]] = {}


def _create_agent_dict(
    agent_id: str,
    name: str,
    prompt: str,
    profile_id: str,
    project_id: Optional[str],
    branch: Optional[str]
) -> Dict[str, Any]:
    """Create a new agent dictionary"""
    now = datetime.utcnow()
    return {
        "id": agent_id,
        "name": name,
        "prompt": prompt,
        "status": "queued",
        "progress": 0.0,
        "profile_id": profile_id,
        "project_id": project_id,
        "branch": branch,
        "tasks": [],
        "logs": [],
        "session_id": None,
        "started_at": None,
        "completed_at": None,
        "error": None,
        "total_cost_usd": 0.0,
        "total_tokens_in": 0,
        "total_tokens_out": 0,
        "created_at": now,
        "updated_at": now,
    }


async def _broadcast_agent_update(agent_id: str, event: Dict[str, Any]):
    """Broadcast an update to all websockets watching this agent"""
    if agent_id not in _agent_websockets:
        return

    disconnected = []
    for ws in _agent_websockets[agent_id]:
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_json(event)
            except Exception as e:
                logger.warning(f"Failed to send to agent websocket: {e}")
                disconnected.append(ws)
        else:
            disconnected.append(ws)

    # Clean up disconnected websockets
    for ws in disconnected:
        _agent_websockets[agent_id].remove(ws)


async def _run_agent_simulation(agent_id: str, prompt: str, profile_id: str, project_id: Optional[str]):
    """
    Simulated agent execution.

    In a real implementation, this would:
    1. Create a Claude session
    2. Execute the prompt with autonomous task management
    3. Track progress and update state
    4. Handle interruptions and resumptions

    For now, this simulates the behavior with progress updates.
    """
    agent = _agents.get(agent_id)
    if not agent:
        return

    try:
        # Update status to running
        agent["status"] = "running"
        agent["started_at"] = datetime.utcnow()
        agent["updated_at"] = datetime.utcnow()
        agent["logs"].append(f"[{datetime.utcnow().isoformat()}] Agent started")

        await _broadcast_agent_update(agent_id, {
            "type": "status_update",
            "agent_id": agent_id,
            "status": "running",
            "progress": 0.0
        })

        # Simulate task breakdown
        tasks = [
            {"id": str(uuid.uuid4()), "name": "Analyzing task requirements", "status": "pending", "children": []},
            {"id": str(uuid.uuid4()), "name": "Planning implementation", "status": "pending", "children": []},
            {"id": str(uuid.uuid4()), "name": "Executing changes", "status": "pending", "children": []},
            {"id": str(uuid.uuid4()), "name": "Verifying results", "status": "pending", "children": []},
        ]
        agent["tasks"] = tasks

        await _broadcast_agent_update(agent_id, {
            "type": "tasks_update",
            "agent_id": agent_id,
            "tasks": tasks
        })

        # Simulate progress through tasks
        for i, task in enumerate(tasks):
            if agent["status"] in ("cancelled", "paused"):
                break

            # Start task
            task["status"] = "in_progress"
            task["started_at"] = datetime.utcnow().isoformat()
            agent["logs"].append(f"[{datetime.utcnow().isoformat()}] Started: {task['name']}")
            agent["progress"] = (i + 0.5) / len(tasks)
            agent["updated_at"] = datetime.utcnow()

            await _broadcast_agent_update(agent_id, {
                "type": "progress_update",
                "agent_id": agent_id,
                "progress": agent["progress"],
                "current_task": task["name"],
                "log": f"Started: {task['name']}"
            })

            # Simulate work (2-5 seconds per task)
            await asyncio.sleep(2 + (i % 3))

            if agent["status"] in ("cancelled", "paused"):
                break

            # Complete task
            task["status"] = "completed"
            task["completed_at"] = datetime.utcnow().isoformat()
            agent["logs"].append(f"[{datetime.utcnow().isoformat()}] Completed: {task['name']}")
            agent["progress"] = (i + 1) / len(tasks)
            agent["updated_at"] = datetime.utcnow()

            await _broadcast_agent_update(agent_id, {
                "type": "progress_update",
                "agent_id": agent_id,
                "progress": agent["progress"],
                "current_task": None if i == len(tasks) - 1 else tasks[i + 1]["name"],
                "log": f"Completed: {task['name']}"
            })

        # Final status
        if agent["status"] == "running":
            agent["status"] = "completed"
            agent["progress"] = 1.0
            agent["completed_at"] = datetime.utcnow()
            agent["logs"].append(f"[{datetime.utcnow().isoformat()}] Agent completed successfully")

            await _broadcast_agent_update(agent_id, {
                "type": "completed",
                "agent_id": agent_id,
                "status": "completed",
                "progress": 1.0
            })

    except asyncio.CancelledError:
        agent["status"] = "cancelled"
        agent["completed_at"] = datetime.utcnow()
        agent["logs"].append(f"[{datetime.utcnow().isoformat()}] Agent cancelled")
        agent["updated_at"] = datetime.utcnow()

        await _broadcast_agent_update(agent_id, {
            "type": "cancelled",
            "agent_id": agent_id,
            "status": "cancelled"
        })

    except Exception as e:
        logger.error(f"Agent {agent_id} failed: {e}", exc_info=True)
        agent["status"] = "failed"
        agent["error"] = str(e)
        agent["completed_at"] = datetime.utcnow()
        agent["logs"].append(f"[{datetime.utcnow().isoformat()}] Agent failed: {e}")
        agent["updated_at"] = datetime.utcnow()

        await _broadcast_agent_update(agent_id, {
            "type": "error",
            "agent_id": agent_id,
            "status": "failed",
            "error": str(e)
        })

    finally:
        # Clean up task reference
        if agent_id in _agent_tasks:
            del _agent_tasks[agent_id]


# ============================================================================
# API Endpoints
# ============================================================================

@router.post("/launch", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def launch_agent(
    request: AgentLaunchRequest,
    req: Request,
    token: str = Depends(require_auth)
):
    """
    Launch a new background agent.

    The agent will execute the given prompt autonomously, optionally creating
    a git branch and PR when complete.
    """
    api_user = get_api_user_from_request(req)

    # Generate agent ID
    agent_id = str(uuid.uuid4())

    # Determine profile
    profile_id = request.profile_id or "claude-code"
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    # Validate project if specified
    project_id = request.project_id
    if project_id:
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

    # Generate branch name if auto_branch
    branch = None
    if request.auto_branch:
        if request.branch_name:
            branch = request.branch_name
        else:
            # Generate branch name from agent name
            safe_name = request.name.lower().replace(" ", "-").replace("_", "-")
            branch = f"agent/{safe_name}-{agent_id[:8]}"

    # Create agent
    agent = _create_agent_dict(
        agent_id=agent_id,
        name=request.name,
        prompt=request.prompt,
        profile_id=profile_id,
        project_id=project_id,
        branch=branch
    )
    _agents[agent_id] = agent

    # Start the agent task
    task = asyncio.create_task(
        _run_agent_simulation(agent_id, request.prompt, profile_id, project_id)
    )
    _agent_tasks[agent_id] = task

    logger.info(f"Launched agent {agent_id}: {request.name}")

    return AgentResponse(**agent)


@router.get("", response_model=List[AgentListResponse])
async def list_agents(
    request: Request,
    status_filter: Optional[str] = Query(None, description="Filter by status"),
    project_id: Optional[str] = Query(None, description="Filter by project"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """List all agents with optional filtering"""
    api_user = get_api_user_from_request(request)

    # Filter agents
    agents = list(_agents.values())

    if status_filter:
        agents = [a for a in agents if a["status"] == status_filter]

    if project_id:
        agents = [a for a in agents if a.get("project_id") == project_id]

    # Sort by created_at descending
    agents.sort(key=lambda a: a["created_at"], reverse=True)

    # Paginate
    agents = agents[offset:offset + limit]

    return [AgentListResponse(
        id=a["id"],
        name=a["name"],
        status=a["status"],
        progress=a["progress"],
        profile_id=a["profile_id"],
        project_id=a.get("project_id"),
        branch=a.get("branch"),
        started_at=a.get("started_at"),
        completed_at=a.get("completed_at"),
        error=a.get("error"),
        created_at=a["created_at"]
    ) for a in agents]


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Get detailed information about a specific agent"""
    agent = _agents.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    return AgentResponse(**agent)


@router.get("/{agent_id}/logs", response_model=AgentLogsResponse)
async def get_agent_logs(
    agent_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Get logs and task breakdown for an agent"""
    agent = _agents.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    return AgentLogsResponse(
        id=agent_id,
        logs=agent.get("logs", []),
        tasks=[AgentTask(**t) for t in agent.get("tasks", [])]
    )


@router.post("/{agent_id}/pause")
async def pause_agent(
    agent_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Pause a running agent"""
    agent = _agents.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    if agent["status"] != "running":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot pause agent in status: {agent['status']}"
        )

    agent["status"] = "paused"
    agent["updated_at"] = datetime.utcnow()
    agent["logs"].append(f"[{datetime.utcnow().isoformat()}] Agent paused")

    await _broadcast_agent_update(agent_id, {
        "type": "status_update",
        "agent_id": agent_id,
        "status": "paused"
    })

    return {"status": "paused", "agent_id": agent_id}


@router.post("/{agent_id}/resume")
async def resume_agent(
    agent_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Resume a paused agent"""
    agent = _agents.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    if agent["status"] != "paused":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot resume agent in status: {agent['status']}"
        )

    agent["status"] = "running"
    agent["updated_at"] = datetime.utcnow()
    agent["logs"].append(f"[{datetime.utcnow().isoformat()}] Agent resumed")

    # Restart the task if needed
    if agent_id not in _agent_tasks:
        task = asyncio.create_task(
            _run_agent_simulation(
                agent_id,
                agent["prompt"],
                agent["profile_id"],
                agent.get("project_id")
            )
        )
        _agent_tasks[agent_id] = task

    await _broadcast_agent_update(agent_id, {
        "type": "status_update",
        "agent_id": agent_id,
        "status": "running"
    })

    return {"status": "running", "agent_id": agent_id}


@router.post("/{agent_id}/cancel")
async def cancel_agent(
    agent_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Cancel a running or queued agent"""
    agent = _agents.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    if agent["status"] in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel agent in status: {agent['status']}"
        )

    # Cancel the task if running
    if agent_id in _agent_tasks:
        _agent_tasks[agent_id].cancel()
        try:
            await _agent_tasks[agent_id]
        except asyncio.CancelledError:
            pass

    agent["status"] = "cancelled"
    agent["completed_at"] = datetime.utcnow()
    agent["updated_at"] = datetime.utcnow()
    agent["logs"].append(f"[{datetime.utcnow().isoformat()}] Agent cancelled by user")

    await _broadcast_agent_update(agent_id, {
        "type": "status_update",
        "agent_id": agent_id,
        "status": "cancelled"
    })

    return {"status": "cancelled", "agent_id": agent_id}


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: str,
    request: Request,
    token: str = Depends(require_admin)
):
    """Delete an agent record (admin only)"""
    agent = _agents.get(agent_id)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_id}"
        )

    # Cancel if still running
    if agent_id in _agent_tasks:
        _agent_tasks[agent_id].cancel()
        try:
            await _agent_tasks[agent_id]
        except asyncio.CancelledError:
            pass
        del _agent_tasks[agent_id]

    # Remove from storage
    del _agents[agent_id]

    # Clean up websockets
    if agent_id in _agent_websockets:
        for ws in _agent_websockets[agent_id]:
            try:
                await ws.close(code=1000, reason="Agent deleted")
            except Exception:
                pass
        del _agent_websockets[agent_id]

    logger.info(f"Deleted agent {agent_id}")


# ============================================================================
# WebSocket for Real-time Updates
# ============================================================================

@router.websocket("/ws")
async def agent_updates_websocket(
    websocket: WebSocket,
    token: str = Query(..., description="Authentication token"),
    agent_id: Optional[str] = Query(None, description="Specific agent to watch")
):
    """
    WebSocket endpoint for real-time agent updates.

    Connect to receive updates for all agents or a specific agent.

    Message types FROM server:
    - status_update: Agent status changed
    - progress_update: Progress changed
    - tasks_update: Task list updated
    - log: New log entry
    - completed: Agent completed
    - error: Agent failed
    - cancelled: Agent cancelled

    Message types TO server:
    - subscribe: Subscribe to a specific agent
    - unsubscribe: Unsubscribe from an agent
    - pong: Response to ping
    """
    # Authenticate
    from app.api.websocket import authenticate_websocket

    await websocket.accept()

    is_authenticated, api_user = await authenticate_websocket(websocket, token)
    if not is_authenticated:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    logger.info(f"Agent WebSocket connected")

    # Track subscriptions
    subscribed_agents: set = set()

    if agent_id:
        if agent_id in _agents:
            subscribed_agents.add(agent_id)
            if agent_id not in _agent_websockets:
                _agent_websockets[agent_id] = []
            _agent_websockets[agent_id].append(websocket)

            # Send current state
            agent = _agents[agent_id]
            await websocket.send_json({
                "type": "state",
                "agent": agent
            })

    try:
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=60.0
                )

                msg_type = data.get("type")

                if msg_type == "subscribe":
                    aid = data.get("agent_id")
                    if aid and aid in _agents:
                        subscribed_agents.add(aid)
                        if aid not in _agent_websockets:
                            _agent_websockets[aid] = []
                        _agent_websockets[aid].append(websocket)

                        # Send current state
                        await websocket.send_json({
                            "type": "state",
                            "agent": _agents[aid]
                        })

                elif msg_type == "unsubscribe":
                    aid = data.get("agent_id")
                    if aid in subscribed_agents:
                        subscribed_agents.discard(aid)
                        if aid in _agent_websockets and websocket in _agent_websockets[aid]:
                            _agent_websockets[aid].remove(websocket)

                elif msg_type == "pong":
                    pass

            except asyncio.TimeoutError:
                if websocket.client_state != WebSocketState.CONNECTED:
                    break
                # Send ping
                await websocket.send_json({"type": "ping"})

    except WebSocketDisconnect:
        logger.info("Agent WebSocket disconnected")

    except Exception as e:
        logger.error(f"Agent WebSocket error: {e}")

    finally:
        # Clean up subscriptions
        for aid in subscribed_agents:
            if aid in _agent_websockets and websocket in _agent_websockets[aid]:
                _agent_websockets[aid].remove(websocket)

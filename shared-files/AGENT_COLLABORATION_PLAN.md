# Agent-to-Agent Collaboration Implementation Plan

## Executive Summary

This document outlines the implementation plan for enabling multiple Claude agents to collaborate on interconnected projects—specifically designed for scenarios like a **TTS Web Server** and **Android Client** that need synchronized API development.

**Feasibility: HIGH** ✅

The existing AI Hub infrastructure provides ~70% of what's needed. Key additions:
1. Inter-agent message bus
2. Shared context store
3. Collaboration orchestration layer
4. Frontend visualization

---

## 1. Architecture Overview

### 1.1 Current State

```
┌─────────────────────────────────────────────────────┐
│                    AI Hub Today                      │
│                                                      │
│  ┌─────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │ Agent A │    │ Query Engine│    │ Agent B     │  │
│  │(Subagent)│←──│ (Spawns)    │──→│ (Subagent)  │  │
│  └─────────┘    └─────────────┘    └─────────────┘  │
│       │                                    │         │
│       └────────── No Communication ────────┘         │
└─────────────────────────────────────────────────────┘
```

### 1.2 Target State

```
┌──────────────────────────────────────────────────────────────┐
│                    AI Hub with Collaboration                  │
│                                                               │
│  ┌─────────────┐    ┌──────────────┐    ┌─────────────────┐  │
│  │  Agent A    │◄──►│ Collab Bus   │◄──►│    Agent B      │  │
│  │ TTS Server  │    │              │    │ Android Client  │  │
│  │ (Session 1) │    │ • Messages   │    │ (Session 2)     │  │
│  └──────┬──────┘    │ • Events     │    └────────┬────────┘  │
│         │           │ • State Sync │             │            │
│         │           └──────────────┘             │            │
│         │                  │                     │            │
│         ▼                  ▼                     ▼            │
│  ┌──────────────────────────────────────────────────────────┐│
│  │              Shared Collaboration Context                 ││
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐   ││
│  │  │ API Contract│  │ Change Log  │  │ Task Board      │   ││
│  │  │ (OpenAPI)   │  │ (Timeline)  │  │ (Assignments)   │   ││
│  │  └─────────────┘  └─────────────┘  └─────────────────┘   ││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
```

---

## 2. Use Case: TTS Server + Android Client

### 2.1 Workflow Example

```
┌─────────────────────────────────────────────────────────────────┐
│ User: "Add a new voice preview endpoint to both apps"           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Orchestrator Agent                            │
│  1. Creates collaboration session                                │
│  2. Assigns tasks to both agents                                 │
│  3. Establishes shared API contract                              │
└─────────────────────────────────────────────────────────────────┘
                              │
            ┌─────────────────┴─────────────────┐
            ▼                                   ▼
┌───────────────────────────┐     ┌───────────────────────────────┐
│   TTS Server Agent        │     │    Android Client Agent       │
│                           │     │                               │
│ 1. Designs endpoint spec  │     │ 1. Waits for API spec         │
│    POST /api/voice/preview│     │                               │
│                           │     │                               │
│ 2. BROADCASTS spec ──────────────────► 2. Receives spec         │
│                           │     │                               │
│ 3. Implements endpoint    │     │ 3. Implements Retrofit call   │
│                           │     │    + VoicePreviewService      │
│                           │     │                               │
│ 4. Writes tests           │     │ 4. Updates UI component       │
│                           │     │                               │
│ 5. NOTIFIES: "Ready" ────────────────► 5. Runs integration test │
│                           │     │                               │
│ 6. ◄─────────────────────────────── REPORTS: "Tests pass"       │
└───────────────────────────┘     └───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Orchestrator: "Both sides implemented. Creating PR summary."    │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 API Contract Management

The shared API contract lives in a collaboration context file:

```yaml
# .collaboration/api-contract.yaml
version: "1.2.0"
updated_by: "tts-server-agent"
updated_at: "2025-01-15T10:30:00Z"

endpoints:
  - path: /api/voice/preview
    method: POST
    added_in: "1.2.0"
    request:
      content_type: application/json
      schema:
        text: string (required, max 500 chars)
        voice_id: string (required)
        speed: number (optional, default 1.0)
    response:
      content_type: audio/wav
      schema:
        binary audio data
    errors:
      - 400: Invalid voice_id
      - 413: Text too long
      - 503: TTS service unavailable

changelog:
  - version: "1.2.0"
    date: "2025-01-15"
    changes:
      - "Added /api/voice/preview endpoint"
      - "Request: text, voice_id, speed"
      - "Response: audio/wav binary"
```

---

## 3. Database Schema

### 3.1 New Tables

```sql
-- Schema version: 12

-- Collaboration sessions (groups of agents working together)
CREATE TABLE collaborations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'active',  -- active, paused, completed, failed
    coordination_mode TEXT DEFAULT 'parallel',  -- sequential, parallel, hierarchical
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    metadata JSON  -- Arbitrary context data
);

-- Agent sessions participating in collaborations
CREATE TABLE collaboration_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collaboration_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_role TEXT,  -- 'coordinator', 'worker', 'reviewer'
    agent_name TEXT,  -- Human-readable name
    project_path TEXT,  -- Workspace path this agent operates in
    status TEXT DEFAULT 'active',  -- active, idle, busy, disconnected
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active_at TIMESTAMP,
    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE(collaboration_id, session_id)
);

-- Inter-agent messages
CREATE TABLE agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collaboration_id TEXT NOT NULL,
    sender_session_id TEXT NOT NULL,
    recipient_session_id TEXT,  -- NULL = broadcast to all
    message_type TEXT NOT NULL,  -- 'api_change', 'task_request', 'status', 'question', 'response'
    subject TEXT,  -- Brief description
    content JSON NOT NULL,  -- Message payload
    priority INTEGER DEFAULT 0,  -- Higher = more urgent
    status TEXT DEFAULT 'pending',  -- pending, delivered, read, acknowledged
    parent_message_id INTEGER,  -- For threading/responses
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered_at TIMESTAMP,
    read_at TIMESTAMP,
    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id) ON DELETE CASCADE,
    FOREIGN KEY (sender_session_id) REFERENCES sessions(id),
    FOREIGN KEY (recipient_session_id) REFERENCES sessions(id)
);

-- Shared context/artifacts
CREATE TABLE collaboration_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collaboration_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,  -- 'api_contract', 'schema', 'config', 'document'
    name TEXT NOT NULL,
    content TEXT NOT NULL,  -- YAML, JSON, or markdown
    version TEXT,
    created_by_session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id) ON DELETE CASCADE,
    UNIQUE(collaboration_id, artifact_type, name)
);

-- Task assignments within collaborations
CREATE TABLE collaboration_tasks (
    id TEXT PRIMARY KEY,
    collaboration_id TEXT NOT NULL,
    assigned_to_session_id TEXT,  -- NULL = unassigned
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'pending',  -- pending, in_progress, blocked, completed, failed
    priority INTEGER DEFAULT 0,
    depends_on JSON,  -- Array of task IDs this depends on
    result JSON,  -- Outcome when completed
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id) ON DELETE CASCADE,
    FOREIGN KEY (assigned_to_session_id) REFERENCES sessions(id)
);

-- Indexes for performance
CREATE INDEX idx_collab_agents_collab ON collaboration_agents(collaboration_id);
CREATE INDEX idx_collab_agents_session ON collaboration_agents(session_id);
CREATE INDEX idx_agent_messages_collab ON agent_messages(collaboration_id);
CREATE INDEX idx_agent_messages_recipient ON agent_messages(recipient_session_id, status);
CREATE INDEX idx_collab_tasks_collab ON collaboration_tasks(collaboration_id);
CREATE INDEX idx_collab_tasks_assignee ON collaboration_tasks(assigned_to_session_id, status);
```

---

## 4. Core Components

### 4.1 Collaboration Manager

**File:** `app/core/collaboration_manager.py`

```python
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from datetime import datetime
import asyncio
import json
from app.db.database import get_db

@dataclass
class CollaborationContext:
    """Context available to agents during collaboration"""
    collaboration_id: str
    name: str
    agents: List[Dict[str, Any]]
    artifacts: Dict[str, str]  # name -> content
    pending_messages: List[Dict[str, Any]]
    active_tasks: List[Dict[str, Any]]


class CollaborationManager:
    """
    Manages multi-agent collaboration sessions.

    Responsibilities:
    - Create/manage collaboration sessions
    - Track participating agents
    - Route inter-agent messages
    - Maintain shared artifacts (API contracts, schemas)
    - Coordinate task assignments
    """

    _instance: Optional['CollaborationManager'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._message_listeners: Dict[str, List[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    # === Collaboration Lifecycle ===

    async def create_collaboration(
        self,
        name: str,
        description: str = "",
        coordination_mode: str = "parallel",
        metadata: Optional[Dict] = None
    ) -> str:
        """Create a new collaboration session"""
        collab_id = f"collab_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name.lower().replace(' ', '_')}"

        db = get_db()
        db.execute(
            """INSERT INTO collaborations
               (id, name, description, coordination_mode, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (collab_id, name, description, coordination_mode,
             json.dumps(metadata or {}))
        )
        db.commit()

        return collab_id

    async def join_collaboration(
        self,
        collaboration_id: str,
        session_id: str,
        agent_role: str = "worker",
        agent_name: str = "",
        project_path: str = ""
    ) -> bool:
        """Add an agent session to a collaboration"""
        db = get_db()
        try:
            db.execute(
                """INSERT INTO collaboration_agents
                   (collaboration_id, session_id, agent_role, agent_name, project_path)
                   VALUES (?, ?, ?, ?, ?)""",
                (collaboration_id, session_id, agent_role, agent_name, project_path)
            )
            db.commit()
            return True
        except Exception:
            return False

    async def leave_collaboration(
        self,
        collaboration_id: str,
        session_id: str
    ) -> bool:
        """Remove an agent from collaboration"""
        db = get_db()
        db.execute(
            """DELETE FROM collaboration_agents
               WHERE collaboration_id = ? AND session_id = ?""",
            (collaboration_id, session_id)
        )
        db.commit()
        return True

    # === Messaging ===

    async def send_message(
        self,
        collaboration_id: str,
        sender_session_id: str,
        message_type: str,
        content: Dict[str, Any],
        recipient_session_id: Optional[str] = None,  # None = broadcast
        subject: str = "",
        priority: int = 0,
        parent_message_id: Optional[int] = None
    ) -> int:
        """Send a message to other agents in the collaboration"""
        db = get_db()
        cursor = db.execute(
            """INSERT INTO agent_messages
               (collaboration_id, sender_session_id, recipient_session_id,
                message_type, subject, content, priority, parent_message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (collaboration_id, sender_session_id, recipient_session_id,
             message_type, subject, json.dumps(content), priority, parent_message_id)
        )
        db.commit()
        message_id = cursor.lastrowid

        # Notify listeners
        await self._notify_message_listeners(collaboration_id, {
            "id": message_id,
            "collaboration_id": collaboration_id,
            "sender_session_id": sender_session_id,
            "recipient_session_id": recipient_session_id,
            "message_type": message_type,
            "subject": subject,
            "content": content,
            "priority": priority
        })

        return message_id

    async def get_pending_messages(
        self,
        collaboration_id: str,
        session_id: str,
        mark_as_read: bool = True
    ) -> List[Dict[str, Any]]:
        """Get unread messages for an agent"""
        db = get_db()
        cursor = db.execute(
            """SELECT * FROM agent_messages
               WHERE collaboration_id = ?
               AND (recipient_session_id = ? OR recipient_session_id IS NULL)
               AND sender_session_id != ?
               AND status IN ('pending', 'delivered')
               ORDER BY priority DESC, created_at ASC""",
            (collaboration_id, session_id, session_id)
        )
        messages = [dict(row) for row in cursor.fetchall()]

        if mark_as_read and messages:
            message_ids = [m['id'] for m in messages]
            db.execute(
                f"""UPDATE agent_messages
                    SET status = 'read', read_at = CURRENT_TIMESTAMP
                    WHERE id IN ({','.join('?' * len(message_ids))})""",
                message_ids
            )
            db.commit()

        return messages

    async def subscribe_to_messages(
        self,
        collaboration_id: str
    ) -> asyncio.Queue:
        """Subscribe to real-time message notifications"""
        async with self._lock:
            if collaboration_id not in self._message_listeners:
                self._message_listeners[collaboration_id] = []
            queue = asyncio.Queue()
            self._message_listeners[collaboration_id].append(queue)
            return queue

    async def unsubscribe_from_messages(
        self,
        collaboration_id: str,
        queue: asyncio.Queue
    ):
        """Unsubscribe from message notifications"""
        async with self._lock:
            if collaboration_id in self._message_listeners:
                self._message_listeners[collaboration_id].remove(queue)

    async def _notify_message_listeners(
        self,
        collaboration_id: str,
        message: Dict[str, Any]
    ):
        """Notify all subscribers of a new message"""
        async with self._lock:
            if collaboration_id in self._message_listeners:
                for queue in self._message_listeners[collaboration_id]:
                    await queue.put(message)

    # === Artifacts (Shared Context) ===

    async def set_artifact(
        self,
        collaboration_id: str,
        artifact_type: str,
        name: str,
        content: str,
        session_id: str,
        version: Optional[str] = None
    ) -> bool:
        """Create or update a shared artifact"""
        db = get_db()
        db.execute(
            """INSERT INTO collaboration_artifacts
               (collaboration_id, artifact_type, name, content, version, created_by_session_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(collaboration_id, artifact_type, name) DO UPDATE SET
               content = excluded.content,
               version = excluded.version,
               updated_at = CURRENT_TIMESTAMP""",
            (collaboration_id, artifact_type, name, content, version, session_id)
        )
        db.commit()

        # Broadcast artifact update
        await self.send_message(
            collaboration_id=collaboration_id,
            sender_session_id=session_id,
            message_type="artifact_updated",
            content={
                "artifact_type": artifact_type,
                "name": name,
                "version": version
            },
            subject=f"Updated {artifact_type}: {name}"
        )

        return True

    async def get_artifact(
        self,
        collaboration_id: str,
        artifact_type: str,
        name: str
    ) -> Optional[str]:
        """Get a shared artifact's content"""
        db = get_db()
        cursor = db.execute(
            """SELECT content FROM collaboration_artifacts
               WHERE collaboration_id = ? AND artifact_type = ? AND name = ?""",
            (collaboration_id, artifact_type, name)
        )
        row = cursor.fetchone()
        return row['content'] if row else None

    async def get_all_artifacts(
        self,
        collaboration_id: str
    ) -> Dict[str, Dict[str, str]]:
        """Get all artifacts for a collaboration"""
        db = get_db()
        cursor = db.execute(
            """SELECT artifact_type, name, content, version
               FROM collaboration_artifacts
               WHERE collaboration_id = ?""",
            (collaboration_id,)
        )
        artifacts = {}
        for row in cursor.fetchall():
            if row['artifact_type'] not in artifacts:
                artifacts[row['artifact_type']] = {}
            artifacts[row['artifact_type']][row['name']] = {
                'content': row['content'],
                'version': row['version']
            }
        return artifacts

    # === Tasks ===

    async def create_task(
        self,
        collaboration_id: str,
        title: str,
        description: str = "",
        assigned_to_session_id: Optional[str] = None,
        priority: int = 0,
        depends_on: Optional[List[str]] = None
    ) -> str:
        """Create a task in the collaboration"""
        task_id = f"task_{datetime.now().strftime('%Y%m%d%H%M%S')}_{hash(title) % 10000}"

        db = get_db()
        db.execute(
            """INSERT INTO collaboration_tasks
               (id, collaboration_id, assigned_to_session_id, title, description, priority, depends_on)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, collaboration_id, assigned_to_session_id, title, description,
             priority, json.dumps(depends_on or []))
        )
        db.commit()

        # Notify if assigned
        if assigned_to_session_id:
            await self.send_message(
                collaboration_id=collaboration_id,
                sender_session_id="system",
                recipient_session_id=assigned_to_session_id,
                message_type="task_assigned",
                content={"task_id": task_id, "title": title, "description": description},
                subject=f"New task: {title}"
            )

        return task_id

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        result: Optional[Dict] = None,
        error_message: Optional[str] = None
    ) -> bool:
        """Update task status"""
        db = get_db()

        updates = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        params = [status]

        if status == "in_progress":
            updates.append("started_at = CURRENT_TIMESTAMP")
        elif status in ("completed", "failed"):
            updates.append("completed_at = CURRENT_TIMESTAMP")

        if result is not None:
            updates.append("result = ?")
            params.append(json.dumps(result))

        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)

        params.append(task_id)

        db.execute(
            f"UPDATE collaboration_tasks SET {', '.join(updates)} WHERE id = ?",
            params
        )
        db.commit()
        return True

    # === Context Building ===

    async def get_collaboration_context(
        self,
        collaboration_id: str,
        session_id: str
    ) -> CollaborationContext:
        """Get full context for an agent in a collaboration"""
        db = get_db()

        # Get collaboration info
        collab = db.execute(
            "SELECT * FROM collaborations WHERE id = ?",
            (collaboration_id,)
        ).fetchone()

        # Get participating agents
        agents = [dict(row) for row in db.execute(
            """SELECT session_id, agent_role, agent_name, project_path, status
               FROM collaboration_agents WHERE collaboration_id = ?""",
            (collaboration_id,)
        ).fetchall()]

        # Get artifacts
        artifacts = await self.get_all_artifacts(collaboration_id)

        # Get pending messages
        messages = await self.get_pending_messages(collaboration_id, session_id, mark_as_read=False)

        # Get active tasks
        tasks = [dict(row) for row in db.execute(
            """SELECT * FROM collaboration_tasks
               WHERE collaboration_id = ? AND status NOT IN ('completed', 'failed')""",
            (collaboration_id,)
        ).fetchall()]

        return CollaborationContext(
            collaboration_id=collaboration_id,
            name=collab['name'] if collab else "",
            agents=agents,
            artifacts=artifacts,
            pending_messages=messages,
            active_tasks=tasks
        )


# Singleton accessor
def get_collaboration_manager() -> CollaborationManager:
    return CollaborationManager()
```

### 4.2 Collaboration Tools for Agents

**File:** `app/core/collaboration_tools.py`

These become available as Claude Agent SDK tools when collaboration is enabled:

```python
"""
Tools available to agents when participating in a collaboration.
These are injected into the agent's tool set during collaboration sessions.
"""

COLLABORATION_TOOLS_PROMPT = """
## Collaboration Tools

You are participating in a multi-agent collaboration. Use these tools to coordinate with other agents:

### send_to_agents
Send a message to other agents in the collaboration.
- Use for: API changes, status updates, questions, requests
- Message types: 'api_change', 'status', 'question', 'request', 'response'

### read_agent_messages
Check for messages from other agents.
- Call periodically to stay in sync
- Returns unread messages with sender info

### update_api_contract
Modify the shared API contract.
- Use when adding/changing endpoints
- Other agents will be notified automatically

### get_api_contract
Read the current API contract.
- Check before implementing client/server code
- Ensure you're using the latest spec

### update_task_status
Report progress on your assigned tasks.
- Status: 'in_progress', 'blocked', 'completed', 'failed'
- Include results or blockers

### get_collaboration_status
Get overview of the collaboration.
- See all agents and their status
- View pending tasks
- Check for blockers
"""

# Tool definitions (Claude Agent SDK format)
COLLABORATION_TOOLS = [
    {
        "name": "send_to_agents",
        "description": "Send a message to other agents in the collaboration. Use message_type: 'api_change' for API updates, 'status' for progress, 'question' for queries, 'request' for asking another agent to do something.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_type": {
                    "type": "string",
                    "enum": ["api_change", "status", "question", "request", "response"],
                    "description": "Type of message"
                },
                "subject": {
                    "type": "string",
                    "description": "Brief subject line"
                },
                "content": {
                    "type": "string",
                    "description": "Message content (markdown supported)"
                },
                "recipient_agent": {
                    "type": "string",
                    "description": "Specific agent name to send to, or omit for broadcast"
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority 0-10, higher is more urgent",
                    "default": 0
                }
            },
            "required": ["message_type", "subject", "content"]
        }
    },
    {
        "name": "read_agent_messages",
        "description": "Read pending messages from other agents. Returns list of unread messages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mark_as_read": {
                    "type": "boolean",
                    "description": "Mark messages as read after retrieval",
                    "default": True
                }
            }
        }
    },
    {
        "name": "update_api_contract",
        "description": "Update the shared API contract. Use YAML format. Other agents will be notified.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contract_yaml": {
                    "type": "string",
                    "description": "Full API contract in YAML format"
                },
                "version": {
                    "type": "string",
                    "description": "Version string (e.g., '1.2.0')"
                },
                "change_summary": {
                    "type": "string",
                    "description": "Brief description of what changed"
                }
            },
            "required": ["contract_yaml", "version", "change_summary"]
        }
    },
    {
        "name": "get_api_contract",
        "description": "Get the current shared API contract.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "update_task_status",
        "description": "Update your task status. Call when starting, completing, or if blocked.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to update"
                },
                "status": {
                    "type": "string",
                    "enum": ["in_progress", "blocked", "completed", "failed"],
                    "description": "New status"
                },
                "result": {
                    "type": "string",
                    "description": "Result or output (for completed tasks)"
                },
                "blocker": {
                    "type": "string",
                    "description": "Description of blocker (for blocked tasks)"
                }
            },
            "required": ["task_id", "status"]
        }
    },
    {
        "name": "get_collaboration_status",
        "description": "Get overview of collaboration: agents, tasks, and current state.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    }
]
```

### 4.3 WebSocket Event Extensions

**File:** `app/api/websocket.py` (additions)

```python
# New event types for collaboration

# FROM server → client
COLLAB_EVENT_TYPES = {
    "collab_message": "New message from another agent",
    "collab_agent_joined": "Agent joined collaboration",
    "collab_agent_left": "Agent left collaboration",
    "collab_artifact_updated": "Shared artifact was updated",
    "collab_task_assigned": "Task assigned to agent",
    "collab_task_updated": "Task status changed",
    "collab_status": "Collaboration status update"
}

async def broadcast_collab_event(
    collaboration_id: str,
    event_type: str,
    data: Dict[str, Any],
    exclude_session_id: Optional[str] = None
):
    """Broadcast collaboration event to all participating agents"""
    db = get_db()

    # Get all sessions in collaboration
    sessions = db.execute(
        "SELECT session_id FROM collaboration_agents WHERE collaboration_id = ?",
        (collaboration_id,)
    ).fetchall()

    for row in sessions:
        session_id = row['session_id']
        if session_id == exclude_session_id:
            continue

        # Use existing sync engine to broadcast
        await sync_engine.broadcast_event(
            session_id=session_id,
            event_type=event_type,
            data={
                "collaboration_id": collaboration_id,
                **data
            }
        )
```

### 4.4 REST API Endpoints

**File:** `app/api/collaboration.py`

```python
from fastapi import APIRouter, HTTPException, Depends
from typing import Optional, List
from pydantic import BaseModel
from app.core.collaboration_manager import get_collaboration_manager
from app.core.auth import get_current_user

router = APIRouter(prefix="/api/v1/collaborations", tags=["collaboration"])

# === Models ===

class CreateCollaborationRequest(BaseModel):
    name: str
    description: str = ""
    coordination_mode: str = "parallel"

class JoinCollaborationRequest(BaseModel):
    session_id: str
    agent_role: str = "worker"
    agent_name: str = ""
    project_path: str = ""

class SendMessageRequest(BaseModel):
    message_type: str
    subject: str
    content: dict
    recipient_session_id: Optional[str] = None
    priority: int = 0

class UpdateArtifactRequest(BaseModel):
    artifact_type: str
    name: str
    content: str
    version: Optional[str] = None

class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    assigned_to_session_id: Optional[str] = None
    priority: int = 0
    depends_on: Optional[List[str]] = None

class UpdateTaskRequest(BaseModel):
    status: str
    result: Optional[dict] = None
    error_message: Optional[str] = None

# === Endpoints ===

@router.post("")
async def create_collaboration(
    request: CreateCollaborationRequest,
    user = Depends(get_current_user)
):
    """Create a new collaboration session"""
    manager = get_collaboration_manager()
    collab_id = await manager.create_collaboration(
        name=request.name,
        description=request.description,
        coordination_mode=request.coordination_mode
    )
    return {"collaboration_id": collab_id}

@router.get("")
async def list_collaborations(
    status: str = "active",
    user = Depends(get_current_user)
):
    """List all collaborations"""
    from app.db.database import get_db
    db = get_db()
    cursor = db.execute(
        "SELECT * FROM collaborations WHERE status = ? ORDER BY created_at DESC",
        (status,)
    )
    return {"collaborations": [dict(row) for row in cursor.fetchall()]}

@router.get("/{collaboration_id}")
async def get_collaboration(
    collaboration_id: str,
    user = Depends(get_current_user)
):
    """Get collaboration details with agents and tasks"""
    manager = get_collaboration_manager()
    from app.db.database import get_db
    db = get_db()

    collab = db.execute(
        "SELECT * FROM collaborations WHERE id = ?",
        (collaboration_id,)
    ).fetchone()

    if not collab:
        raise HTTPException(status_code=404, detail="Collaboration not found")

    agents = db.execute(
        "SELECT * FROM collaboration_agents WHERE collaboration_id = ?",
        (collaboration_id,)
    ).fetchall()

    tasks = db.execute(
        "SELECT * FROM collaboration_tasks WHERE collaboration_id = ?",
        (collaboration_id,)
    ).fetchall()

    artifacts = await manager.get_all_artifacts(collaboration_id)

    return {
        "collaboration": dict(collab),
        "agents": [dict(a) for a in agents],
        "tasks": [dict(t) for t in tasks],
        "artifacts": artifacts
    }

@router.post("/{collaboration_id}/join")
async def join_collaboration(
    collaboration_id: str,
    request: JoinCollaborationRequest,
    user = Depends(get_current_user)
):
    """Add an agent to a collaboration"""
    manager = get_collaboration_manager()
    success = await manager.join_collaboration(
        collaboration_id=collaboration_id,
        session_id=request.session_id,
        agent_role=request.agent_role,
        agent_name=request.agent_name,
        project_path=request.project_path
    )
    if not success:
        raise HTTPException(status_code=400, detail="Failed to join collaboration")
    return {"status": "joined"}

@router.post("/{collaboration_id}/leave")
async def leave_collaboration(
    collaboration_id: str,
    session_id: str,
    user = Depends(get_current_user)
):
    """Remove an agent from a collaboration"""
    manager = get_collaboration_manager()
    await manager.leave_collaboration(collaboration_id, session_id)
    return {"status": "left"}

@router.post("/{collaboration_id}/messages")
async def send_message(
    collaboration_id: str,
    request: SendMessageRequest,
    session_id: str,  # Query param: sender session
    user = Depends(get_current_user)
):
    """Send a message to agents in the collaboration"""
    manager = get_collaboration_manager()
    message_id = await manager.send_message(
        collaboration_id=collaboration_id,
        sender_session_id=session_id,
        message_type=request.message_type,
        subject=request.subject,
        content=request.content,
        recipient_session_id=request.recipient_session_id,
        priority=request.priority
    )
    return {"message_id": message_id}

@router.get("/{collaboration_id}/messages")
async def get_messages(
    collaboration_id: str,
    session_id: str,
    mark_as_read: bool = True,
    user = Depends(get_current_user)
):
    """Get pending messages for an agent"""
    manager = get_collaboration_manager()
    messages = await manager.get_pending_messages(
        collaboration_id=collaboration_id,
        session_id=session_id,
        mark_as_read=mark_as_read
    )
    return {"messages": messages}

@router.put("/{collaboration_id}/artifacts")
async def update_artifact(
    collaboration_id: str,
    request: UpdateArtifactRequest,
    session_id: str,
    user = Depends(get_current_user)
):
    """Create or update a shared artifact"""
    manager = get_collaboration_manager()
    await manager.set_artifact(
        collaboration_id=collaboration_id,
        artifact_type=request.artifact_type,
        name=request.name,
        content=request.content,
        session_id=session_id,
        version=request.version
    )
    return {"status": "updated"}

@router.get("/{collaboration_id}/artifacts/{artifact_type}/{name}")
async def get_artifact(
    collaboration_id: str,
    artifact_type: str,
    name: str,
    user = Depends(get_current_user)
):
    """Get a specific artifact"""
    manager = get_collaboration_manager()
    content = await manager.get_artifact(collaboration_id, artifact_type, name)
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return {"content": content}

@router.post("/{collaboration_id}/tasks")
async def create_task(
    collaboration_id: str,
    request: CreateTaskRequest,
    user = Depends(get_current_user)
):
    """Create a task in the collaboration"""
    manager = get_collaboration_manager()
    task_id = await manager.create_task(
        collaboration_id=collaboration_id,
        title=request.title,
        description=request.description,
        assigned_to_session_id=request.assigned_to_session_id,
        priority=request.priority,
        depends_on=request.depends_on
    )
    return {"task_id": task_id}

@router.patch("/{collaboration_id}/tasks/{task_id}")
async def update_task(
    collaboration_id: str,
    task_id: str,
    request: UpdateTaskRequest,
    user = Depends(get_current_user)
):
    """Update task status"""
    manager = get_collaboration_manager()
    await manager.update_task_status(
        task_id=task_id,
        status=request.status,
        result=request.result,
        error_message=request.error_message
    )
    return {"status": "updated"}
```

---

## 5. Query Engine Integration

### 5.1 Collaboration Context Injection

When an agent session is part of a collaboration, inject context into the system prompt:

**File:** `app/core/query_engine.py` (modifications)

```python
async def build_collaboration_context(session_id: str) -> Optional[str]:
    """Build collaboration context for system prompt injection"""
    from app.core.collaboration_manager import get_collaboration_manager
    from app.db.database import get_db

    db = get_db()

    # Check if session is in any active collaboration
    collab = db.execute(
        """SELECT c.*, ca.agent_role, ca.agent_name
           FROM collaborations c
           JOIN collaboration_agents ca ON c.id = ca.collaboration_id
           WHERE ca.session_id = ? AND c.status = 'active'""",
        (session_id,)
    ).fetchone()

    if not collab:
        return None

    manager = get_collaboration_manager()
    context = await manager.get_collaboration_context(collab['id'], session_id)

    # Build context string
    lines = [
        "## Active Collaboration",
        f"**Name:** {context.name}",
        f"**Your Role:** {collab['agent_role']}",
        f"**Your Name:** {collab['agent_name']}",
        "",
        "### Other Agents:",
    ]

    for agent in context.agents:
        if agent['session_id'] != session_id:
            lines.append(f"- **{agent['agent_name']}** ({agent['agent_role']}): {agent['project_path']}")

    # Add API contract if exists
    if 'api_contract' in context.artifacts:
        api_contract = context.artifacts['api_contract'].get('main', {})
        if api_contract:
            lines.extend([
                "",
                "### Shared API Contract:",
                "```yaml",
                api_contract.get('content', ''),
                "```"
            ])

    # Add pending messages
    if context.pending_messages:
        lines.extend([
            "",
            f"### Pending Messages ({len(context.pending_messages)}):",
        ])
        for msg in context.pending_messages[:5]:  # Show first 5
            lines.append(f"- [{msg['message_type']}] from {msg['sender_session_id']}: {msg['subject']}")

    # Add active tasks
    my_tasks = [t for t in context.active_tasks if t['assigned_to_session_id'] == session_id]
    if my_tasks:
        lines.extend([
            "",
            "### Your Tasks:",
        ])
        for task in my_tasks:
            lines.append(f"- [{task['status']}] {task['title']}")

    lines.extend([
        "",
        "Use the collaboration tools (send_to_agents, read_agent_messages, etc.) to coordinate with other agents.",
    ])

    return "\n".join(lines)


# In build_options_from_profile(), add collaboration tools when in collaboration:
async def build_options_from_profile(
    profile_id: str,
    session_id: str,
    # ... existing params ...
) -> ClaudeAgentOptions:
    # ... existing code ...

    # Check for collaboration
    collab_context = await build_collaboration_context(session_id)
    if collab_context:
        # Append to system prompt
        system_prompt = (system_prompt or "") + "\n\n" + collab_context

        # Add collaboration tools
        from app.core.collaboration_tools import COLLABORATION_TOOLS
        # Register tools with SDK...

    # ... rest of function ...
```

### 5.2 Tool Execution Handlers

**File:** `app/core/collaboration_tool_handlers.py`

```python
"""
Handlers for collaboration tool execution.
Called by Claude Agent SDK when agent uses collaboration tools.
"""

from typing import Dict, Any, Optional
from app.core.collaboration_manager import get_collaboration_manager

async def handle_send_to_agents(
    session_id: str,
    collaboration_id: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle send_to_agents tool call"""
    manager = get_collaboration_manager()

    # Find recipient session ID if agent name provided
    recipient_session_id = None
    if params.get('recipient_agent'):
        from app.db.database import get_db
        db = get_db()
        recipient = db.execute(
            """SELECT session_id FROM collaboration_agents
               WHERE collaboration_id = ? AND agent_name = ?""",
            (collaboration_id, params['recipient_agent'])
        ).fetchone()
        if recipient:
            recipient_session_id = recipient['session_id']

    message_id = await manager.send_message(
        collaboration_id=collaboration_id,
        sender_session_id=session_id,
        message_type=params['message_type'],
        subject=params['subject'],
        content={"text": params['content']},
        recipient_session_id=recipient_session_id,
        priority=params.get('priority', 0)
    )

    return {
        "success": True,
        "message_id": message_id,
        "recipients": "all agents" if not recipient_session_id else params['recipient_agent']
    }


async def handle_read_agent_messages(
    session_id: str,
    collaboration_id: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle read_agent_messages tool call"""
    manager = get_collaboration_manager()
    messages = await manager.get_pending_messages(
        collaboration_id=collaboration_id,
        session_id=session_id,
        mark_as_read=params.get('mark_as_read', True)
    )

    # Format for agent consumption
    formatted = []
    for msg in messages:
        formatted.append({
            "id": msg['id'],
            "from": msg['sender_session_id'],  # Could map to agent name
            "type": msg['message_type'],
            "subject": msg['subject'],
            "content": msg['content'],
            "priority": msg['priority'],
            "sent_at": msg['created_at']
        })

    return {
        "message_count": len(formatted),
        "messages": formatted
    }


async def handle_update_api_contract(
    session_id: str,
    collaboration_id: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle update_api_contract tool call"""
    manager = get_collaboration_manager()

    await manager.set_artifact(
        collaboration_id=collaboration_id,
        artifact_type="api_contract",
        name="main",
        content=params['contract_yaml'],
        session_id=session_id,
        version=params.get('version')
    )

    # Send notification with change summary
    await manager.send_message(
        collaboration_id=collaboration_id,
        sender_session_id=session_id,
        message_type="api_change",
        subject=f"API Contract updated to v{params.get('version', 'unknown')}",
        content={
            "version": params.get('version'),
            "summary": params.get('change_summary', ''),
            "action_required": "Please review and update your implementation accordingly."
        },
        priority=5  # Higher priority for API changes
    )

    return {
        "success": True,
        "version": params.get('version'),
        "notification_sent": True
    }


async def handle_get_api_contract(
    session_id: str,
    collaboration_id: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle get_api_contract tool call"""
    manager = get_collaboration_manager()
    content = await manager.get_artifact(
        collaboration_id=collaboration_id,
        artifact_type="api_contract",
        name="main"
    )

    if content:
        return {"contract": content}
    else:
        return {"contract": None, "message": "No API contract defined yet"}


async def handle_update_task_status(
    session_id: str,
    collaboration_id: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle update_task_status tool call"""
    manager = get_collaboration_manager()

    result = None
    if params.get('result'):
        result = {"output": params['result']}

    error_message = params.get('blocker') if params['status'] == 'blocked' else None

    await manager.update_task_status(
        task_id=params['task_id'],
        status=params['status'],
        result=result,
        error_message=error_message
    )

    # Notify others if blocked
    if params['status'] == 'blocked':
        await manager.send_message(
            collaboration_id=collaboration_id,
            sender_session_id=session_id,
            message_type="status",
            subject=f"Task blocked: {params['task_id']}",
            content={
                "task_id": params['task_id'],
                "blocker": params.get('blocker', 'Unknown blocker'),
                "help_needed": True
            },
            priority=7
        )

    return {"success": True, "status": params['status']}


async def handle_get_collaboration_status(
    session_id: str,
    collaboration_id: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle get_collaboration_status tool call"""
    manager = get_collaboration_manager()
    context = await manager.get_collaboration_context(collaboration_id, session_id)

    return {
        "collaboration_name": context.name,
        "agents": [
            {
                "name": a.get('agent_name', 'Unknown'),
                "role": a.get('agent_role', 'worker'),
                "project": a.get('project_path', ''),
                "status": a.get('status', 'unknown')
            }
            for a in context.agents
        ],
        "pending_messages": len(context.pending_messages),
        "active_tasks": [
            {
                "id": t['id'],
                "title": t['title'],
                "status": t['status'],
                "assigned_to": t.get('assigned_to_session_id')
            }
            for t in context.active_tasks
        ],
        "artifacts": list(context.artifacts.keys())
    }
```

---

## 6. Frontend Components

### 6.1 Collaboration Panel

**File:** `frontend/src/lib/components/CollaborationPanel.svelte`

```svelte
<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { collaborationStore } from '$lib/stores/collaboration';

  export let sessionId: string;

  let collaboration = $state(null);
  let messages = $state([]);
  let agents = $state([]);

  onMount(async () => {
    // Load collaboration state
    collaboration = await collaborationStore.loadForSession(sessionId);
    if (collaboration) {
      messages = collaboration.pending_messages;
      agents = collaboration.agents;
    }
  });
</script>

{#if collaboration}
  <div class="collaboration-panel">
    <div class="panel-header">
      <h3>🤝 {collaboration.name}</h3>
      <span class="status-badge">{collaboration.status}</span>
    </div>

    <!-- Agents -->
    <div class="agents-section">
      <h4>Agents</h4>
      {#each agents as agent}
        <div class="agent-card" class:current={agent.session_id === sessionId}>
          <span class="agent-status" class:active={agent.status === 'active'}></span>
          <span class="agent-name">{agent.agent_name}</span>
          <span class="agent-role">({agent.agent_role})</span>
        </div>
      {/each}
    </div>

    <!-- Messages -->
    <div class="messages-section">
      <h4>Messages ({messages.length})</h4>
      {#each messages as msg}
        <div class="message-card" class:high-priority={msg.priority > 5}>
          <div class="message-header">
            <span class="message-type">{msg.message_type}</span>
            <span class="message-from">from {msg.sender_session_id}</span>
          </div>
          <div class="message-subject">{msg.subject}</div>
        </div>
      {/each}
    </div>

    <!-- Tasks -->
    <div class="tasks-section">
      <h4>Tasks</h4>
      {#each collaboration.tasks as task}
        <div class="task-card" class:my-task={task.assigned_to_session_id === sessionId}>
          <span class="task-status">{task.status}</span>
          <span class="task-title">{task.title}</span>
        </div>
      {/each}
    </div>
  </div>
{/if}

<style>
  .collaboration-panel {
    background: var(--surface-2);
    border-radius: 8px;
    padding: 1rem;
    margin: 1rem 0;
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }

  .agent-card {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem;
    background: var(--surface-3);
    border-radius: 4px;
    margin-bottom: 0.25rem;
  }

  .agent-card.current {
    border-left: 3px solid var(--accent);
  }

  .agent-status {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-muted);
  }

  .agent-status.active {
    background: var(--success);
  }

  .message-card {
    padding: 0.5rem;
    background: var(--surface-3);
    border-radius: 4px;
    margin-bottom: 0.25rem;
  }

  .message-card.high-priority {
    border-left: 3px solid var(--warning);
  }

  .task-card {
    padding: 0.5rem;
    background: var(--surface-3);
    border-radius: 4px;
    margin-bottom: 0.25rem;
  }

  .task-card.my-task {
    border-left: 3px solid var(--accent);
  }
</style>
```

### 6.2 Collaboration Store

**File:** `frontend/src/lib/stores/collaboration.ts`

```typescript
import { writable, derived } from 'svelte/store';
import { api } from '$lib/api';

interface Agent {
  session_id: string;
  agent_name: string;
  agent_role: string;
  project_path: string;
  status: string;
}

interface Message {
  id: number;
  sender_session_id: string;
  message_type: string;
  subject: string;
  content: any;
  priority: number;
  created_at: string;
}

interface Task {
  id: string;
  title: string;
  status: string;
  assigned_to_session_id: string;
}

interface Collaboration {
  id: string;
  name: string;
  status: string;
  agents: Agent[];
  tasks: Task[];
  pending_messages: Message[];
  artifacts: Record<string, any>;
}

function createCollaborationStore() {
  const { subscribe, set, update } = writable<Collaboration | null>(null);

  return {
    subscribe,

    async loadForSession(sessionId: string): Promise<Collaboration | null> {
      try {
        const response = await api.get(`/api/v1/sessions/${sessionId}/collaboration`);
        if (response.collaboration) {
          set(response.collaboration);
          return response.collaboration;
        }
        return null;
      } catch (e) {
        return null;
      }
    },

    async create(name: string, description: string = ''): Promise<string> {
      const response = await api.post('/api/v1/collaborations', {
        name,
        description,
        coordination_mode: 'parallel'
      });
      return response.collaboration_id;
    },

    async join(collaborationId: string, sessionId: string, agentName: string, role: string = 'worker') {
      await api.post(`/api/v1/collaborations/${collaborationId}/join`, {
        session_id: sessionId,
        agent_name: agentName,
        agent_role: role
      });
      await this.loadForSession(sessionId);
    },

    async sendMessage(collaborationId: string, sessionId: string, messageType: string, subject: string, content: any) {
      await api.post(`/api/v1/collaborations/${collaborationId}/messages?session_id=${sessionId}`, {
        message_type: messageType,
        subject,
        content
      });
    },

    addMessage(message: Message) {
      update(collab => {
        if (collab) {
          return {
            ...collab,
            pending_messages: [...collab.pending_messages, message]
          };
        }
        return collab;
      });
    },

    clear() {
      set(null);
    }
  };
}

export const collaborationStore = createCollaborationStore();
```

---

## 7. Implementation Phases

### Phase 1: Foundation (Week 1-2)

**Tasks:**
1. ✅ Design database schema
2. ⬜ Add database migrations (schema version 12)
3. ⬜ Implement `CollaborationManager` class
4. ⬜ Create REST API endpoints
5. ⬜ Basic unit tests

**Deliverables:**
- Collaborations can be created/joined via API
- Messages can be sent between sessions
- Artifacts can be stored and retrieved

### Phase 2: Agent Integration (Week 2-3)

**Tasks:**
1. ⬜ Define collaboration tools schema
2. ⬜ Implement tool handlers
3. ⬜ Integrate with `build_options_from_profile()`
4. ⬜ Add collaboration context to system prompt
5. ⬜ WebSocket event extensions

**Deliverables:**
- Agents can use collaboration tools
- Context automatically injected when in collaboration
- Real-time message delivery via WebSocket

### Phase 3: Frontend & UX (Week 3-4)

**Tasks:**
1. ⬜ Create `CollaborationPanel` component
2. ⬜ Add collaboration store
3. ⬜ Integrate with main chat UI
4. ⬜ Add collaboration creation flow
5. ⬜ Message visualization

**Deliverables:**
- Users can create/manage collaborations from UI
- Visual feedback on agent communication
- Task board visualization

### Phase 4: Polish & Testing (Week 4-5)

**Tasks:**
1. ⬜ End-to-end integration tests
2. ⬜ Performance optimization
3. ⬜ Error handling improvements
4. ⬜ Documentation
5. ⬜ Example workflows (TTS + Android)

**Deliverables:**
- Production-ready collaboration feature
- Example templates for common use cases
- User documentation

---

## 8. Example: TTS Server + Android Client

### 8.1 Setup Script

```bash
# Create collaboration
curl -X POST http://localhost:8000/api/v1/collaborations \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Vibe Voice TTS + Android",
    "description": "Coordinated development of TTS server and Android client",
    "coordination_mode": "parallel"
  }'

# Returns: { "collaboration_id": "collab_20250115_vibe_voice_tts_android" }

# Join TTS Server session
curl -X POST http://localhost:8000/api/v1/collaborations/collab_20250115_vibe_voice_tts_android/join \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session_tts_server",
    "agent_name": "TTS Server Agent",
    "agent_role": "worker",
    "project_path": "/workspace/vibe-voice-tts"
  }'

# Join Android session
curl -X POST http://localhost:8000/api/v1/collaborations/collab_20250115_vibe_voice_tts_android/join \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session_android_client",
    "agent_name": "Android Client Agent",
    "agent_role": "worker",
    "project_path": "/workspace/vibe-reader-android"
  }'
```

### 8.2 Initial API Contract

```yaml
# Set via API or agent tool
version: "1.0.0"
base_url: "https://tts.example.com/api/v1"

endpoints:
  - path: /voices
    method: GET
    description: List available voices
    response:
      type: array
      items:
        id: string
        name: string
        language: string
        gender: string
        preview_url: string

  - path: /synthesize
    method: POST
    description: Convert text to speech
    request:
      text: string (required, max 5000 chars)
      voice_id: string (required)
      speed: number (optional, 0.5-2.0, default 1.0)
      format: string (optional, "mp3"|"wav", default "mp3")
    response:
      audio_url: string
      duration_seconds: number
      characters_processed: number

error_codes:
  - 400: Bad request (invalid parameters)
  - 401: Unauthorized
  - 413: Text too long
  - 429: Rate limited
  - 503: TTS service unavailable
```

### 8.3 Agent Interaction Example

**TTS Server Agent** (after implementing new endpoint):
```
Tool: send_to_agents
{
  "message_type": "api_change",
  "subject": "Added /api/v1/voices/{id}/preview endpoint",
  "content": "New endpoint for voice preview. Returns 5-second audio sample. Request: GET /api/v1/voices/{id}/preview. Response: audio/wav binary. Added to API contract v1.1.0.",
  "priority": 5
}
```

**Android Client Agent** (receives message):
```
[Reading message...]
Received API change notification. New preview endpoint available.

Tool: get_api_contract
[Gets updated contract]

Now implementing VoicePreviewService with Retrofit...
```

---

## 9. Success Metrics

| Metric | Target |
|--------|--------|
| Message delivery latency | < 100ms |
| Agent context load time | < 500ms |
| Max agents per collaboration | 10 |
| Max messages per collaboration | 10,000 |
| Collaboration creation success rate | > 99% |

---

## 10. Open Questions

1. **SDK Support**: Does Claude Agent SDK natively support inter-agent communication, or do we implement entirely in AI Hub?

2. **Message Ordering**: Should we guarantee strict ordering of messages, or is eventual consistency acceptable?

3. **Artifact Conflicts**: How to handle simultaneous API contract updates from multiple agents?

4. **Session Persistence**: Should collaborations survive session restarts?

5. **Access Control**: Should agents be able to limit which other agents can message them?

---

## 11. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Message storms (agents spamming each other) | High | Rate limiting per agent, priority queues |
| Stale context (agent working with old info) | Medium | Mandatory context refresh before major actions |
| Coordination deadlocks | Medium | Timeout mechanisms, coordinator agent pattern |
| Database growth from messages | Low | Message archival, TTL on old messages |

---

## Conclusion

Implementing agent-to-agent collaboration in AI Hub is **highly feasible** and would enable powerful workflows like synchronized TTS server + Android client development.

The existing infrastructure provides:
- ✅ Multi-session management
- ✅ Real-time WebSocket communication
- ✅ Flexible tool system
- ✅ Session state persistence

Required additions:
- 📦 Collaboration database tables (~200 lines SQL)
- 📦 CollaborationManager class (~400 lines Python)
- 📦 Collaboration tools for agents (~150 lines Python)
- 📦 REST API endpoints (~200 lines Python)
- 📦 Frontend components (~300 lines Svelte)

**Estimated total effort: 3-5 weeks** for a production-ready implementation.

The recommended approach is to start with Phase 1 (database + manager) to validate the core messaging system, then progressively add agent tools and UI.

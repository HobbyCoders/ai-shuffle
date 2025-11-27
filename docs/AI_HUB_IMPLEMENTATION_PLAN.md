# AI Hub Implementation Plan

> Transforming the Claude SDK Proxy into a full-featured AI Hub with Claude Code Web UI and OpenAI-compatible API

## Overview

This document outlines the complete architecture and implementation plan for evolving the current proof-of-concept into a comprehensive AI Hub that serves two main purposes:

1. **Claude Code Web** - A full-featured web interface like claude.ai/code
2. **OpenAI-compatible API** - A proxy server for other applications to utilize

## Current State

The current application is a thin FastAPI proxy that wraps the Python Agent SDK (which itself wraps Claude Code CLI):

```
Your App (REST) → Python Agent SDK → Claude Code CLI → Anthropic API
```

**Current features:**
- Pure API service, no frontend
- Read-only (dangerous tools blocked via `disallowed_tools`)
- Stateless queries + per-request stateful conversations
- Uses `ClaudeAgentOptions` with the `"claude_code"` preset
- Authentication via Claude Code CLI OAuth

---

## Target Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                           Docker Container                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│   Port 8000                                                         │
│       │                                                             │
│       ▼                                                             │
│   ┌─────────────────────────────────────────────────────────────┐  │
│   │                    FastAPI Application                       │  │
│   │                                                              │  │
│   │  Static Files (/app/static)  ←── Svelte Build Output        │  │
│   │       │                                                      │  │
│   │       ├── /                    → Svelte SPA                 │  │
│   │       ├── /api/v1/*            → REST API                   │  │
│   │       ├── /api/v1/stream/*     → SSE Streaming              │  │
│   │       └── /v1/chat/completions → OpenAI Compat (optional)   │  │
│   │                                                              │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                          │                                          │
│                          ▼                                          │
│   ┌─────────────────────────────────────────────────────────────┐  │
│   │                     Core Services                            │  │
│   │                                                              │  │
│   │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │  │
│   │  │   Profile    │ │   Session    │ │   Project    │        │  │
│   │  │   Service    │ │   Service    │ │   Service    │        │  │
│   │  └──────────────┘ └──────────────┘ └──────────────┘        │  │
│   │                                                              │  │
│   │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │  │
│   │  │    Auth      │ │  MCP Tool    │ │   Query      │        │  │
│   │  │   Service    │ │   Service    │ │   Engine     │        │  │
│   │  └──────────────┘ └──────────────┘ └──────────────┘        │  │
│   │                                                              │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                          │                                          │
│                          ▼                                          │
│   ┌─────────────────────────────────────────────────────────────┐  │
│   │                   Claude Agent SDK                           │  │
│   │                                                              │  │
│   │  query() / ClaudeSDKClient                                  │  │
│   │       │                                                      │  │
│   │       ▼                                                      │  │
│   │  Claude Code CLI (npm: @anthropic-ai/claude-code)           │  │
│   │       │                                                      │  │
│   │       ▼                                                      │  │
│   │  Anthropic API                                               │  │
│   │                                                              │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                                                                     │
│   Volumes:                                                          │
│   ├── /data/db.sqlite         ← SQLite database                    │
│   ├── /data/sessions/         ← Session conversation logs          │
│   ├── /home/appuser/.claude/  ← Claude OAuth credentials           │
│   └── /workspace/             ← Project files                      │
│       ├── project-a/                                               │
│       ├── project-b/                                               │
│       └── ...                                                      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Core Concept: Agent Profiles

The central abstraction is **Agent Profiles** - pre-configured `ClaudeAgentOptions` with varying capability levels:

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent Profile System                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Profile: "simple-chat"          Profile: "code-reader"     │
│  ├─ allowed_tools: []            ├─ allowed_tools: [Read,   │
│  ├─ permission_mode: default     │   Glob, Grep]            │
│  ├─ model: haiku                 ├─ permission_mode: default│
│  └─ No file access               └─ Read-only access        │
│                                                             │
│  Profile: "code-writer"          Profile: "full-claude"     │
│  ├─ allowed_tools: [Read,Write,  ├─ allowed_tools: [ALL]    │
│  │   Edit,Bash,Glob,Grep]        ├─ permission_mode: bypass │
│  ├─ permission_mode: acceptEdits ├─ mcp_servers: {...}      │
│  └─ Can modify files             └─ Full Claude Code        │
│                                                             │
│  Profile: "data-extractor"       Profile: [custom...]       │
│  ├─ allowed_tools: [WebFetch]    ├─ User-defined            │
│  ├─ system_prompt: "JSON only"   └─ Via Web UI              │
│  └─ Specialized for scraping                                │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## API Contract

### Authentication Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/auth/setup` | First-launch admin creation |
| POST | `/api/v1/auth/login` | Login, returns session cookie |
| POST | `/api/v1/auth/logout` | Invalidate session |
| GET | `/api/v1/auth/status` | Check auth status + Claude CLI auth |

### Profile Endpoints (Agent Configurations)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/profiles` | List all profiles |
| GET | `/api/v1/profiles/:id` | Get profile details |
| POST | `/api/v1/profiles` | Create custom profile |
| PUT | `/api/v1/profiles/:id` | Update profile |
| DELETE | `/api/v1/profiles/:id` | Delete profile (not built-ins) |

### Project Endpoints (Workspaces)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/projects` | List all projects |
| GET | `/api/v1/projects/:id` | Get project details |
| POST | `/api/v1/projects` | Create project (creates /workspace/x) |
| PUT | `/api/v1/projects/:id` | Update project settings |
| DELETE | `/api/v1/projects/:id` | Delete project |

### Session Endpoints (Conversations)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/sessions` | List sessions (with filters) |
| GET | `/api/v1/sessions/:id` | Get session with history |
| DELETE | `/api/v1/sessions/:id` | Delete session |

### Query Endpoints (Main AI Interface)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/query` | One-shot query (stateless) |
| POST | `/api/v1/query/stream` | SSE streaming query |
| POST | `/api/v1/conversation` | Multi-turn (creates/continues session) |
| GET | `/api/v1/conversation/:id/stream` | SSE for existing session |

### MCP Tool Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/mcp/tools` | List available custom tools |
| POST | `/api/v1/mcp/tools` | Register new tool (future) |

### System Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/health` | Health check |
| GET | `/api/v1/version` | Version info |
| GET | `/api/v1/stats` | Usage stats, costs |

### OpenAI Compatibility (Optional)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/chat/completions` | Maps to simple-chat profile |
| GET | `/v1/models` | List available models |

### Request/Response Examples

#### Query Request
```json
{
    "prompt": "Explain this code",
    "profile": "code-reader",
    "project": "my-app",
    "overrides": {
        "model": "claude-opus",
        "system_prompt_append": "Be concise"
    }
}
```

#### Query Response
```json
{
    "response": "...",
    "session_id": "abc123",
    "metadata": {
        "model": "claude-opus-4-20250514",
        "duration_ms": 2500,
        "total_cost_usd": 0.05,
        "tokens": {"input": 500, "output": 200}
    }
}
```

#### SSE Stream Format
```
event: message
data: {"type": "text", "content": "Here's "}

event: message
data: {"type": "text", "content": "the explanation..."}

event: tool_use
data: {"type": "tool", "name": "Read", "input": {"file": "/workspace/x/main.py"}}

event: tool_result
data: {"type": "tool_result", "name": "Read", "output": "...file contents..."}

event: done
data: {"session_id": "abc123", "metadata": {...}}
```

---

## Database Schema (SQLite)

```sql
-- Admin user (single user system)
CREATE TABLE admin (
    id INTEGER PRIMARY KEY DEFAULT 1,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,  -- bcrypt
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (id = 1)  -- Enforce single row
);

-- Agent profiles
CREATE TABLE profiles (
    id TEXT PRIMARY KEY,              -- e.g., "kavita-scraper"
    name TEXT NOT NULL,               -- Display name
    description TEXT,
    is_builtin BOOLEAN DEFAULT FALSE, -- Can't delete built-ins
    config JSON NOT NULL,             -- ClaudeAgentOptions as JSON
    mcp_tools JSON DEFAULT '[]',      -- List of MCP tool IDs to attach
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Projects (workspaces)
CREATE TABLE projects (
    id TEXT PRIMARY KEY,              -- e.g., "my-app"
    name TEXT NOT NULL,
    description TEXT,
    path TEXT NOT NULL,               -- Relative to /workspace, e.g., "my-app"
    default_profile_id TEXT,          -- Default profile for this project
    settings JSON DEFAULT '{}',       -- Project-specific settings
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (default_profile_id) REFERENCES profiles(id)
);

-- Sessions (conversations)
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,              -- UUID
    project_id TEXT,                  -- Optional project association
    profile_id TEXT NOT NULL,
    sdk_session_id TEXT,              -- Agent SDK's session ID for resume
    title TEXT,                       -- Auto-generated or user-set
    status TEXT DEFAULT 'active',     -- active, archived
    total_cost_usd REAL DEFAULT 0,
    total_tokens_in INTEGER DEFAULT 0,
    total_tokens_out INTEGER DEFAULT 0,
    turn_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (profile_id) REFERENCES profiles(id)
);

-- Session messages (conversation history for display)
CREATE TABLE session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,               -- user, assistant, system, tool
    content TEXT NOT NULL,            -- Message content
    tool_name TEXT,                   -- If role=tool
    tool_input JSON,                  -- If role=tool
    metadata JSON,                    -- Cost, tokens, etc.
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

-- User auth sessions (login tokens)
CREATE TABLE auth_sessions (
    token TEXT PRIMARY KEY,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Usage tracking
CREATE TABLE usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    profile_id TEXT,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX idx_sessions_project ON sessions(project_id);
CREATE INDEX idx_sessions_status ON sessions(status);
CREATE INDEX idx_session_messages_session ON session_messages(session_id);
CREATE INDEX idx_usage_log_created ON usage_log(created_at);
```

---

## Built-in Agent Profiles

```python
BUILTIN_PROFILES = {
    "simple-chat": {
        "id": "simple-chat",
        "name": "Simple Chat",
        "description": "Text-only responses, no tool access",
        "is_builtin": True,
        "config": {
            "model": "claude-sonnet-4",
            "allowed_tools": [],
            "permission_mode": "default",
            "max_turns": 10,
            "system_prompt": None
        }
    },

    "code-reader": {
        "id": "code-reader",
        "name": "Code Reader",
        "description": "Read-only code analysis, no modifications",
        "is_builtin": True,
        "config": {
            "model": "claude-sonnet-4",
            "allowed_tools": ["Read", "Glob", "Grep"],
            "permission_mode": "default",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code"
            }
        }
    },

    "code-writer": {
        "id": "code-writer",
        "name": "Code Writer",
        "description": "Can read and write files, auto-approves edits",
        "is_builtin": True,
        "config": {
            "model": "claude-sonnet-4",
            "allowed_tools": ["Read", "Write", "Edit", "Glob", "Grep"],
            "permission_mode": "acceptEdits",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code"
            }
        }
    },

    "full-claude": {
        "id": "full-claude",
        "name": "Full Claude Code",
        "description": "Complete Claude Code experience with all tools",
        "is_builtin": True,
        "config": {
            "model": "claude-sonnet-4",
            "allowed_tools": [
                "Read", "Write", "Edit", "Glob", "Grep", "Bash",
                "WebFetch", "WebSearch", "NotebookEdit", "TodoWrite",
                "Task", "BashOutput", "KillShell"
            ],
            "permission_mode": "bypassPermissions",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code"
            },
            "setting_sources": ["project"]
        }
    },

    "data-extractor": {
        "id": "data-extractor",
        "name": "Data Extractor",
        "description": "Optimized for structured data extraction (JSON output)",
        "is_builtin": True,
        "config": {
            "model": "claude-haiku-4",
            "allowed_tools": ["WebFetch"],
            "permission_mode": "bypassPermissions",
            "max_turns": 3,
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                "append": "Return ONLY valid JSON. No markdown, no explanations, no code blocks. Pure JSON output."
            }
        }
    },

    "researcher": {
        "id": "researcher",
        "name": "Researcher",
        "description": "Web search and content analysis",
        "is_builtin": True,
        "config": {
            "model": "claude-sonnet-4",
            "allowed_tools": ["Read", "Glob", "Grep", "WebFetch", "WebSearch"],
            "permission_mode": "default",
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                "append": "Always cite sources and provide references."
            }
        }
    }
}
```

### Profile to ClaudeAgentOptions Conversion

```python
def profile_to_options(profile: dict, project: dict = None) -> ClaudeAgentOptions:
    """Convert stored profile to SDK options"""
    config = profile["config"]

    options = ClaudeAgentOptions(
        model=config.get("model"),
        allowed_tools=config.get("allowed_tools"),
        disallowed_tools=config.get("disallowed_tools"),
        permission_mode=config.get("permission_mode"),
        max_turns=config.get("max_turns"),
        system_prompt=config.get("system_prompt"),
        setting_sources=config.get("setting_sources"),
    )

    # Apply project context
    if project:
        options.cwd = f"/workspace/{project['path']}"

    # Attach MCP tools if specified
    if profile.get("mcp_tools"):
        options.mcp_servers = load_mcp_servers(profile["mcp_tools"])

    return options
```

---

## Session Management & Streaming

### Session Flow

```python
# 1. New conversation starts
POST /api/v1/conversation
{
    "prompt": "Help me refactor auth.py",
    "profile": "code-writer",
    "project": "my-app"
}

# Server:
# - Creates session record in DB
# - Builds ClaudeAgentOptions from profile
# - Calls query() with streaming
# - Stores SDK session_id for resume
# - Streams responses via SSE

# 2. Continue existing conversation
POST /api/v1/conversation
{
    "session_id": "abc123",
    "prompt": "Now add tests for that"
}

# Server:
# - Loads session from DB
# - Uses ClaudeAgentOptions(resume=sdk_session_id)
# - Continues conversation with full context
```

### Streaming Implementation

```python
async def stream_query(request, response: StreamingResponse):
    """SSE streaming endpoint"""

    session = create_or_load_session(request)
    options = build_options(session)

    async def event_generator():
        async for message in query(prompt=request.prompt, options=options):

            if isinstance(message, SystemMessage):
                if message.subtype == "init":
                    # Store SDK session ID for resume
                    session.sdk_session_id = message.session_id
                    yield sse_event("init", {"session_id": session.id})

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        yield sse_event("text", {"content": block.text})
                        store_message(session.id, "assistant", block.text)

                    elif isinstance(block, ToolUseBlock):
                        yield sse_event("tool_use", {
                            "name": block.name,
                            "input": block.input
                        })

                    elif isinstance(block, ToolResultBlock):
                        yield sse_event("tool_result", {
                            "name": block.name,
                            "output": block.content[:1000]
                        })

            elif isinstance(message, ResultMessage):
                session.total_cost_usd += message.total_cost_usd
                session.turn_count += message.num_turns
                save_session(session)

                yield sse_event("done", {
                    "session_id": session.id,
                    "metadata": {
                        "cost": message.total_cost_usd,
                        "turns": message.num_turns,
                        "duration_ms": message.duration_ms
                    }
                })

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


def sse_event(event_type: str, data: dict) -> str:
    """Format SSE event"""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
```

---

## Svelte UI Structure

```
frontend/
├── src/
│   ├── lib/
│   │   ├── api/
│   │   │   ├── client.ts          # API client (fetch wrapper)
│   │   │   ├── auth.ts            # Auth endpoints
│   │   │   ├── profiles.ts        # Profile CRUD
│   │   │   ├── projects.ts        # Project CRUD
│   │   │   ├── sessions.ts        # Session management
│   │   │   └── query.ts           # Query/stream endpoints
│   │   │
│   │   ├── components/
│   │   │   ├── layout/
│   │   │   │   ├── Sidebar.svelte
│   │   │   │   ├── Header.svelte
│   │   │   │   └── Layout.svelte
│   │   │   │
│   │   │   ├── chat/
│   │   │   │   ├── ChatWindow.svelte
│   │   │   │   ├── MessageList.svelte
│   │   │   │   ├── Message.svelte
│   │   │   │   ├── ToolUse.svelte
│   │   │   │   ├── InputArea.svelte
│   │   │   │   └── StreamingText.svelte
│   │   │   │
│   │   │   ├── terminal/
│   │   │   │   ├── Terminal.svelte
│   │   │   │   ├── FileTree.svelte
│   │   │   │   └── CodeViewer.svelte
│   │   │   │
│   │   │   ├── profiles/
│   │   │   │   ├── ProfileList.svelte
│   │   │   │   ├── ProfileCard.svelte
│   │   │   │   ├── ProfileEditor.svelte
│   │   │   │   └── ProfileSelector.svelte
│   │   │   │
│   │   │   ├── projects/
│   │   │   │   ├── ProjectList.svelte
│   │   │   │   ├── ProjectCard.svelte
│   │   │   │   └── ProjectSettings.svelte
│   │   │   │
│   │   │   ├── sessions/
│   │   │   │   ├── SessionList.svelte
│   │   │   │   └── SessionCard.svelte
│   │   │   │
│   │   │   └── common/
│   │   │       ├── Modal.svelte
│   │   │       ├── Button.svelte
│   │   │       ├── Input.svelte
│   │   │       ├── Select.svelte
│   │   │       ├── Toast.svelte
│   │   │       └── Loading.svelte
│   │   │
│   │   ├── stores/
│   │   │   ├── auth.ts
│   │   │   ├── session.ts
│   │   │   ├── profiles.ts
│   │   │   ├── projects.ts
│   │   │   └── ui.ts
│   │   │
│   │   └── utils/
│   │       ├── sse.ts
│   │       ├── markdown.ts
│   │       └── syntax.ts
│   │
│   ├── routes/
│   │   ├── +layout.svelte
│   │   ├── +page.svelte
│   │   ├── login/+page.svelte
│   │   ├── setup/+page.svelte
│   │   ├── chat/
│   │   │   ├── +page.svelte
│   │   │   └── [id]/+page.svelte
│   │   ├── terminal/+page.svelte
│   │   ├── profiles/
│   │   │   ├── +page.svelte
│   │   │   └── [id]/+page.svelte
│   │   ├── projects/
│   │   │   ├── +page.svelte
│   │   │   └── [id]/+page.svelte
│   │   └── settings/+page.svelte
│   │
│   ├── app.html
│   ├── app.css
│   └── app.d.ts
│
├── static/
├── svelte.config.js
├── tailwind.config.js
├── vite.config.js
└── package.json
```

### Main Chat Interface Design

```
┌────────────────────────────────────────────────────────┐
│  [Profile: Full Claude] [Project: my-app] [Settings]   │
├────────────────────────────────────────────────────────┤
│                                                        │
│  User                                                  │
│  Help me refactor the auth module                      │
│                                                        │
│  Claude                                                │
│  I'll analyze the current auth implementation...       │
│                                                        │
│  ┌──────────────────────────────────────────────────┐ │
│  │ Read: /workspace/my-app/src/auth.py              │ │
│  │ ──────────────────────────────────────────────── │ │
│  │ def authenticate(user, password):                 │ │
│  │     # ... file contents ...                       │ │
│  └──────────────────────────────────────────────────┘ │
│                                                        │
│  I see several issues. Let me fix them:               │
│                                                        │
│  ┌──────────────────────────────────────────────────┐ │
│  │ Edit: /workspace/my-app/src/auth.py               │ │
│  │ Lines 15-20 modified                              │ │
│  └──────────────────────────────────────────────────┘ │
│                                                        │
│  ┌──────────────────────────────────────────────────┐ │
│  │ Bash: pytest tests/test_auth.py                  │ │
│  │ ──────────────────────────────────────────────── │ │
│  │ 5 passed in 0.42s                                │ │
│  └──────────────────────────────────────────────────┘ │
│                                                        │
│  Done! I've refactored the auth module and tests pass.│
│                                                        │
├────────────────────────────────────────────────────────┤
│  [                    Type a message...              ] │
│  Cost: $0.05 | Turns: 3                       [Send]  │
└────────────────────────────────────────────────────────┘
```

---

## Security Model

### Authentication

```python
# First-run setup detection
def is_setup_required() -> bool:
    return db.query("SELECT COUNT(*) FROM admin")[0] == 0

# Setup endpoint (only works if no admin exists)
@app.post("/api/v1/auth/setup")
async def setup_admin(username: str, password: str):
    if not is_setup_required():
        raise HTTPException(403, "Admin already configured")

    hashed = bcrypt.hash(password)
    db.execute("INSERT INTO admin (username, password_hash) VALUES (?, ?)",
               [username, hashed])

    return create_session_token()

# Login with session cookie
@app.post("/api/v1/auth/login")
async def login(username: str, password: str, response: Response):
    admin = db.query("SELECT * FROM admin WHERE username = ?", [username])
    if not admin or not bcrypt.verify(password, admin.password_hash):
        raise HTTPException(401, "Invalid credentials")

    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(days=30)

    db.execute("INSERT INTO auth_sessions (token, expires_at) VALUES (?, ?)",
               [token, expires])

    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=30 * 24 * 60 * 60
    )

    return {"status": "ok"}

# Auth middleware
async def require_auth(request: Request):
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Not authenticated")

    session = db.query(
        "SELECT * FROM auth_sessions WHERE token = ? AND expires_at > ?",
        [token, datetime.utcnow()]
    )
    if not session:
        raise HTTPException(401, "Session expired")
```

### Path Validation (Workspace Isolation)

```python
WORKSPACE_ROOT = Path("/workspace")

def validate_project_path(path: str) -> Path:
    """Ensure path is within workspace and normalized"""

    full_path = (WORKSPACE_ROOT / path).resolve()

    if not str(full_path).startswith(str(WORKSPACE_ROOT.resolve())):
        raise HTTPException(400, "Path escapes workspace boundary")

    return full_path

def create_project(project_id: str):
    """Create project directory safely"""
    path = validate_project_path(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
```

### Agent SDK Security Hooks

```python
async def path_validation_hook(tool_name: str, tool_input: dict) -> dict:
    """PreToolUse hook to validate file paths"""

    path_params = ["file_path", "path", "directory"]

    for param in path_params:
        if param in tool_input:
            path = tool_input[param]

            try:
                resolved = Path(path).resolve()
                if not str(resolved).startswith("/workspace"):
                    return {
                        "decision": "block",
                        "reason": f"Path {path} is outside workspace"
                    }
            except Exception:
                return {
                    "decision": "block",
                    "reason": f"Invalid path: {path}"
                }

    return {"decision": "allow"}

async def dangerous_command_hook(tool_name: str, tool_input: dict) -> dict:
    """Block dangerous bash commands"""

    if tool_name != "Bash":
        return {"decision": "allow"}

    command = tool_input.get("command", "")

    dangerous_patterns = [
        r"rm\s+-rf\s+/",
        r":\(\)\{.*\}",
        r">\s*/dev/sd",
        r"mkfs\.",
        r"dd\s+.*of=/dev",
    ]

    for pattern in dangerous_patterns:
        if re.search(pattern, command):
            return {
                "decision": "block",
                "reason": "Potentially dangerous command blocked"
            }

    return {"decision": "allow"}

# Apply hooks to options
def build_secure_options(profile: dict, project: dict) -> ClaudeAgentOptions:
    options = profile_to_options(profile, project)

    options.hooks = {
        "PreToolUse": [
            HookMatcher(hooks=[path_validation_hook]),
            HookMatcher(matcher="Bash", hooks=[dangerous_command_hook])
        ]
    }

    if project:
        options.cwd = f"/workspace/{project['path']}"
        options.add_dirs = []

    return options
```

---

## Implementation Phases

### Phase 1: Core API Foundation
**Priority: HIGH** (enables everything else)

- [ ] Restructure project layout
  - `/app/api/` → FastAPI routes
  - `/app/core/` → Services, models
  - `/app/db/` → SQLite + migrations
  - `/frontend/` → Svelte app
- [ ] Database setup
  - SQLite initialization
  - Schema creation
  - Built-in profile seeding
- [ ] Auth system
  - First-run setup flow
  - Login/logout
  - Session middleware
- [ ] Profile system
  - Built-in profiles
  - CRUD endpoints
  - Profile → ClaudeAgentOptions conversion
- [ ] Basic query endpoint
  - POST /api/v1/query (non-streaming)
  - Profile selection + overrides

**Deliverable:** Working API that can execute queries with profiles

---

### Phase 2: Streaming & Sessions
**Priority: HIGH** (core functionality)

- [ ] SSE streaming implementation
  - Stream query responses
  - Tool use events
  - Progress/status events
- [ ] Session management
  - Create/store sessions
  - Resume sessions (SDK session_id)
  - Message history storage
  - Cost/usage tracking
- [ ] Project system
  - Project CRUD
  - Workspace directory management
  - Project ↔ Session linking

**Deliverable:** Full conversational API with streaming and persistence

---

### Phase 3: Svelte UI - Foundation
**Priority: HIGH** (user experience)

- [ ] Svelte/SvelteKit setup
  - Project scaffolding
  - Tailwind CSS
  - Build integration with FastAPI
- [ ] Auth UI
  - Setup page (first run)
  - Login page
  - Auth state management
- [ ] Basic layout
  - Sidebar navigation
  - Header
  - Responsive design
- [ ] Chat interface (basic)
  - Message display
  - Input area
  - Profile selector
  - SSE streaming display

**Deliverable:** Functional web UI for chatting with Claude

---

### Phase 4: Full Claude Code Experience
**Priority: MEDIUM** (enhancement)

- [ ] Rich tool display
  - File read visualization
  - Code diff viewer (edits)
  - Terminal output (bash)
  - Collapsible tool blocks
- [ ] File browser
  - Project file tree
  - File preview
  - Integration with chat
- [ ] Profile management UI
  - Profile list view
  - Profile editor (JSON/form)
  - Tool selection interface
- [ ] Project management UI
  - Project list
  - Create/configure projects
  - Project-specific settings

**Deliverable:** Full-featured Claude Code web experience

---

### Phase 5: Polish & Advanced Features
**Priority: LOW** (nice to have)

- [ ] OpenAI compatibility layer
  - /v1/chat/completions endpoint
  - Model mapping
  - Response format translation
- [ ] MCP tool management
  - SDK MCP tool definitions
  - Tool attachment to profiles
  - Custom tool UI
- [ ] Usage dashboard
  - Cost tracking charts
  - Usage by profile/project
  - Session analytics
- [ ] UI enhancements
  - Dark/light theme
  - Keyboard shortcuts
  - Mobile responsive
  - Export conversations
- [ ] Docker improvements
  - Multi-arch builds
  - Health checks
  - Unraid template update

**Deliverable:** Production-ready, polished application

---

## Key Design Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Database | SQLite | Simple, file-based, easy backup, no extra containers |
| Profile storage | Database + JSON | Managed via API/UI |
| Project isolation | Path restrictions | Single user, lighter weight than container-per-project |
| Auth for Web UI | Session-based (cookie) | Single user, simple security |
| MCP approach | SDK MCP (in-process) | No container dependencies needed |
| OpenAI compat | Optional thin layer | Nice-to-have, not primary focus |
| Frontend | Custom Svelte | Modern, lightweight, good DX |
| Streaming | SSE (Server-Sent Events) | Real-time Claude output |
| Workspace | Single /workspace volume | Subdirectories per project |

---

## MCP Servers Note

MCP servers can be added without modifying the container by using **SDK MCP Servers** (in-process):

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("search_manga", "Search manga databases", {"query": str})
async def search_manga(args):
    # Custom logic here
    return {"content": [{"type": "text", "text": "results..."}]}

custom_server = create_sdk_mcp_server(
    name="manga-tools",
    version="1.0.0",
    tools=[search_manga]
)

# Use in profile config
options = ClaudeAgentOptions(
    mcp_servers={"manga-tools": custom_server},
    allowed_tools=["mcp__manga-tools__search_manga"]
)
```

This allows defining custom tools in Python that are immediately available without external processes.

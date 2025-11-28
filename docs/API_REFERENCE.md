# AI Hub API Reference

Complete API documentation for integrating with AI Hub, a web interface for Claude Code.

## Base URL

```
http://localhost:8000/api/v1
```

## Authentication

All API endpoints (except health check) require authentication via session cookie.

### Authentication Flow

1. **First Launch**: If no admin exists, call `/auth/setup` to create one
2. **Login**: Call `/auth/login` to get a session cookie
3. **Subsequent Requests**: Include the `session` cookie in all requests

### Cookie-Based Auth

The API uses HTTP-only cookies for session management. After login, the `session` cookie is automatically included in subsequent requests.

---

## Authentication Endpoints

### GET /api/v1/auth/status

Get current authentication status.

**Authentication**: None required

**Response**:
```json
{
  "authenticated": true,
  "setup_required": false,
  "claude_authenticated": true,
  "username": "admin"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `authenticated` | boolean | Whether user is logged into the web UI |
| `setup_required` | boolean | Whether initial admin setup is needed |
| `claude_authenticated` | boolean | Whether Claude CLI is authenticated |
| `username` | string\|null | Logged-in username if authenticated |

---

### POST /api/v1/auth/setup

Create the initial admin account. Only works if no admin exists.

**Authentication**: None required

**Request Body**:
```json
{
  "username": "admin",
  "password": "securepassword123"
}
```

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `username` | string | Yes | 3-50 characters |
| `password` | string | Yes | Minimum 8 characters |

**Response**:
```json
{
  "status": "ok",
  "message": "Admin account created",
  "username": "admin"
}
```

**Errors**:
- `403`: Admin already configured

---

### POST /api/v1/auth/login

Login and receive session cookie.

**Authentication**: None required

**Request Body**:
```json
{
  "username": "admin",
  "password": "yourpassword"
}
```

**Response**:
```json
{
  "status": "ok",
  "message": "Logged in"
}
```

**Response Headers**: Sets `session` cookie

**Errors**:
- `401`: Invalid credentials

---

### POST /api/v1/auth/logout

Logout and invalidate session.

**Authentication**: Optional (clears cookie regardless)

**Response**:
```json
{
  "status": "ok",
  "message": "Logged out"
}
```

---

### GET /api/v1/auth/claude/status

Get Claude CLI authentication status.

**Authentication**: Required

**Response**:
```json
{
  "authenticated": true,
  "method": "api_key"
}
```

---

### GET /api/v1/auth/claude/login-instructions

Get instructions for authenticating Claude CLI.

**Authentication**: Required

**Response**:
```json
{
  "instructions": "Run 'claude login' in the container terminal..."
}
```

---

### GET /api/v1/auth/diagnostics

Run diagnostic checks for authentication issues.

**Authentication**: Required

**Response**:
```json
{
  "home_env": "/home/appuser",
  "claude_path": "/usr/local/bin/claude",
  "claude_dir": "/home/appuser/.claude",
  "claude_dir_exists": true,
  "claude_credentials_exists": true,
  "credentials_file_size": 256,
  "process_user": "appuser",
  "process_uid": 1000
}
```

---

## Query Endpoints

These are the main AI interaction endpoints.

### POST /api/v1/query

One-shot query - stateless, creates a new session each time.

**Authentication**: Required + Claude CLI must be authenticated

**Request Body**:
```json
{
  "prompt": "Write a hello world function in Python",
  "profile": "claude-code",
  "project": "my-project",
  "overrides": {
    "model": "opus",
    "system_prompt_append": "Always include type hints",
    "max_turns": 5
  }
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `prompt` | string | Yes | - | The query/instruction |
| `profile` | string | No | "claude-code" | Profile ID to use |
| `project` | string | No | null | Project ID for working directory |
| `overrides` | object | No | null | Runtime overrides (see below) |

**Overrides Object**:
| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Override model (claude-sonnet-4, claude-opus-4, claude-haiku-3-5) |
| `system_prompt_append` | string | Additional instructions to append |
| `max_turns` | integer | Maximum conversation turns |

**Response**:
```json
{
  "response": "Here's a hello world function in Python:\n\n```python\ndef hello_world() -> str:\n    return \"Hello, World!\"\n```",
  "session_id": "sess_abc123",
  "metadata": {
    "model": "sonnet",
    "duration_ms": 1523,
    "total_cost_usd": 0.0042,
    "tokens_in": 150,
    "tokens_out": 89,
    "num_turns": 1
  }
}
```

---

### POST /api/v1/query/stream

SSE streaming one-shot query.

**Authentication**: Required + Claude CLI must be authenticated

**Request Body**: Same as `/query`

**Response**: Server-Sent Events stream (see SSE Events section)

---

### POST /api/v1/conversation

Multi-turn conversation with session persistence.

**Authentication**: Required + Claude CLI must be authenticated

**Request Body**:
```json
{
  "prompt": "Now add error handling to that function",
  "session_id": "sess_abc123",
  "profile": "claude-code",
  "project": "my-project",
  "overrides": null
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | Yes | The message |
| `session_id` | string | No | Existing session to continue (null = new session) |
| `profile` | string | No | Profile for new sessions (default: "claude-code") |
| `project` | string | No | Project for new sessions |
| `overrides` | object | No | Runtime overrides |

**Response**: Same as `/query`

---

### POST /api/v1/conversation/stream

SSE streaming conversation.

**Authentication**: Required + Claude CLI must be authenticated

**Request Body**: Same as `/conversation`

**Response**: Server-Sent Events stream (see SSE Events section)

---

### POST /api/v1/session/{session_id}/interrupt

Interrupt an active streaming session.

**Authentication**: Required

**Path Parameters**:
| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | string | Session ID to interrupt |

**Response**:
```json
{
  "status": "interrupted",
  "session_id": "sess_abc123"
}
```

**Errors**:
- `404`: No active session found

---

### GET /api/v1/sessions/active

List currently active streaming sessions.

**Authentication**: Required

**Response**:
```json
{
  "active_sessions": ["sess_abc123", "sess_def456"]
}
```

---

## SSE Event Types

When using streaming endpoints, you receive Server-Sent Events:

### init
Sent at stream start with session information.
```json
{
  "type": "init",
  "session_id": "sess_abc123"
}
```

### text
Text content chunks.
```json
{
  "type": "text",
  "content": "Here's the "
}
```

### tool_use
Tool invocation by Claude.
```json
{
  "type": "tool_use",
  "name": "Write",
  "input": {
    "file_path": "/workspace/hello.py",
    "content": "def hello(): pass"
  }
}
```

### tool_result
Tool execution result.
```json
{
  "type": "tool_result",
  "name": "Write",
  "output": "File written successfully"
}
```

### done
Stream completion.
```json
{
  "type": "done",
  "session_id": "sess_abc123",
  "metadata": {
    "model": "sonnet",
    "duration_ms": 5234,
    "total_cost_usd": 0.0156,
    "tokens_in": 450,
    "tokens_out": 312,
    "num_turns": 3
  }
}
```

### interrupted
Session was interrupted.
```json
{
  "type": "interrupted"
}
```

### error
An error occurred.
```json
{
  "type": "error",
  "message": "Claude CLI not authenticated"
}
```

---

## Profile Endpoints

### GET /api/v1/profiles

List all profiles.

**Authentication**: Required

**Response**:
```json
[
  {
    "id": "claude-code",
    "name": "Claude Code",
    "description": "Default coding assistant profile",
    "config": {
      "model": "sonnet",
      "permission_mode": "default"
    },
    "is_builtin": true,
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z"
  }
]
```

---

### GET /api/v1/profiles/{profile_id}

Get a specific profile.

**Authentication**: Required

**Response**: Single profile object

**Errors**:
- `404`: Profile not found

---

### POST /api/v1/profiles

Create a custom profile.

**Authentication**: Required

**Request Body**:
```json
{
  "id": "my-custom-profile",
  "name": "My Custom Profile",
  "description": "Specialized for frontend work",
  "config": {
    "model": "sonnet",
    "permission_mode": "default",
    "max_turns": 10,
    "allowed_tools": ["Read", "Write", "Bash"],
    "disallowed_tools": null,
    "system_prompt": {
      "type": "custom",
      "content": "You are a frontend specialist."
    },
    "cwd": "/workspace/frontend",
    "add_dirs": ["/workspace/shared"],
    "include_partial_messages": true,
    "continue_conversation": false,
    "fork_session": false,
    "setting_sources": ["user", "project"],
    "env": {
      "NODE_ENV": "development"
    }
  }
}
```

**Profile Config Options**:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | "sonnet" | Model to use: sonnet, opus, haiku |
| `permission_mode` | string | "default" | Permission mode: default, acceptEdits, plan, bypassPermissions |
| `max_turns` | integer | null | Maximum conversation turns |
| `allowed_tools` | string[] | null | Whitelist of allowed tools |
| `disallowed_tools` | string[] | null | Blacklist of disallowed tools |
| `system_prompt` | object | null | System prompt configuration |
| `include_partial_messages` | boolean | false | Enable streaming partial messages |
| `continue_conversation` | boolean | false | Continue most recent conversation |
| `fork_session` | boolean | false | Fork instead of continuing when resuming |
| `cwd` | string | null | Override working directory |
| `add_dirs` | string[] | null | Additional accessible directories |
| `setting_sources` | string[] | null | Settings to load: user, project, local |
| `env` | object | null | Environment variables |
| `extra_args` | object | null | Additional CLI arguments |
| `max_buffer_size` | integer | null | Max bytes for CLI stdout buffer |
| `user` | string | null | User identifier |

**System Prompt Config**:
```json
{
  "type": "preset",
  "preset": "claude_code"
}
```
or
```json
{
  "type": "custom",
  "content": "Your custom system prompt here",
  "append": "Additional instructions appended to base prompt"
}
```

**Response**: Created profile object (status 201)

**Errors**:
- `409`: Profile already exists

---

### PUT /api/v1/profiles/{profile_id}

Update a profile (cannot modify built-ins).

**Authentication**: Required

**Request Body**: Same as create, but all fields optional

**Errors**:
- `403`: Cannot modify built-in profiles
- `404`: Profile not found

---

### DELETE /api/v1/profiles/{profile_id}

Delete a profile (cannot delete built-ins).

**Authentication**: Required

**Response**: 204 No Content

**Errors**:
- `403`: Cannot delete built-in profiles
- `404`: Profile not found

---

## Project Endpoints

### GET /api/v1/projects

List all projects.

**Authentication**: Required

**Response**:
```json
[
  {
    "id": "my-project",
    "name": "My Project",
    "description": "A sample project",
    "path": "my-project",
    "settings": {
      "default_profile_id": "claude-code",
      "custom_instructions": "Focus on TypeScript"
    },
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z"
  }
]
```

---

### GET /api/v1/projects/{project_id}

Get a specific project.

**Authentication**: Required

**Errors**:
- `404`: Project not found

---

### POST /api/v1/projects

Create a new project.

**Authentication**: Required

**Request Body**:
```json
{
  "id": "my-new-project",
  "name": "My New Project",
  "description": "Project description",
  "settings": {
    "default_profile_id": "claude-code",
    "custom_instructions": "Additional context for Claude"
  }
}
```

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `id` | string | Yes | Lowercase alphanumeric + hyphens, 1-50 chars |
| `name` | string | Yes | 1-100 characters |
| `description` | string | No | - |
| `settings` | object | No | Project settings |

**Project Settings**:
| Field | Type | Description |
|-------|------|-------------|
| `default_profile_id` | string | Default profile for this project |
| `custom_instructions` | string | Additional context for Claude |

**Response**: Created project (status 201)

**Errors**:
- `409`: Project already exists

---

### PUT /api/v1/projects/{project_id}

Update a project.

**Authentication**: Required

**Request Body**: Same as create, but all fields optional

---

### DELETE /api/v1/projects/{project_id}

Delete a project (database record only, not files).

**Authentication**: Required

**Response**: 204 No Content

---

### GET /api/v1/projects/{project_id}/files

List files in a project directory.

**Authentication**: Required

**Query Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | "" | Subdirectory path to list |

**Response**:
```json
{
  "files": [
    {
      "name": "src",
      "type": "directory",
      "size": null,
      "path": "src"
    },
    {
      "name": "README.md",
      "type": "file",
      "size": 1234,
      "path": "README.md"
    }
  ],
  "path": ""
}
```

---

## Session Endpoints

### GET /api/v1/sessions

List sessions with optional filters.

**Authentication**: Required

**Query Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project_id` | string | null | Filter by project |
| `profile_id` | string | null | Filter by profile |
| `status` | string | null | Filter by status (active, completed, archived) |
| `limit` | integer | 50 | Max results (1-100) |
| `offset` | integer | 0 | Pagination offset |

**Response**:
```json
[
  {
    "id": "sess_abc123",
    "profile_id": "claude-code",
    "project_id": "my-project",
    "title": "Hello world function",
    "sdk_session_id": "sdk_xyz789",
    "status": "completed",
    "total_cost_usd": 0.0156,
    "total_tokens_in": 450,
    "total_tokens_out": 312,
    "turn_count": 3,
    "created_at": "2024-01-01T12:00:00Z",
    "updated_at": "2024-01-01T12:05:00Z"
  }
]
```

---

### GET /api/v1/sessions/{session_id}

Get a session with its message history.

**Authentication**: Required

**Response**:
```json
{
  "id": "sess_abc123",
  "profile_id": "claude-code",
  "project_id": "my-project",
  "title": "Hello world function",
  "sdk_session_id": "sdk_xyz789",
  "status": "completed",
  "total_cost_usd": 0.0156,
  "total_tokens_in": 450,
  "total_tokens_out": 312,
  "turn_count": 3,
  "created_at": "2024-01-01T12:00:00Z",
  "updated_at": "2024-01-01T12:05:00Z",
  "messages": [
    {
      "id": 1,
      "role": "user",
      "content": "Write a hello world function",
      "tool_name": null,
      "tool_input": null,
      "metadata": null,
      "created_at": "2024-01-01T12:00:00Z"
    },
    {
      "id": 2,
      "role": "assistant",
      "content": "Here's a hello world function...",
      "tool_name": null,
      "tool_input": null,
      "metadata": {"model": "claude-sonnet-4"},
      "created_at": "2024-01-01T12:00:05Z"
    }
  ]
}
```

---

### PATCH /api/v1/sessions/{session_id}

Update session title or status.

**Authentication**: Required

**Query Parameters**:
| Parameter | Type | Description |
|-----------|------|-------------|
| `title` | string | New session title |
| `status` | string | New status (active, completed, archived) |

**Response**: Updated session object

---

### DELETE /api/v1/sessions/{session_id}

Delete a session and its messages.

**Authentication**: Required

**Response**: 204 No Content

---

### POST /api/v1/sessions/{session_id}/archive

Archive a session.

**Authentication**: Required

**Response**:
```json
{
  "status": "ok",
  "message": "Session archived"
}
```

---

## System Endpoints

### GET /health

Health check (no auth required).

**Response**:
```json
{
  "status": "healthy",
  "service": "ai-hub",
  "version": "2.1.0",
  "authenticated": false,
  "setup_required": false,
  "claude_authenticated": true
}
```

---

### GET /api/v1/health

API health check (alias for /health).

---

### GET /api/v1/version

Get version information.

**Authentication**: None required

**Response**:
```json
{
  "api_version": "2.1.0",
  "claude_version": "1.0.0"
}
```

---

### GET /api/v1/stats

Get usage statistics.

**Authentication**: Required

**Response**:
```json
{
  "total_sessions": 150,
  "total_queries": 523,
  "total_cost_usd": 12.45,
  "total_tokens_in": 125000,
  "total_tokens_out": 89000
}
```

---

## Integration Examples

### Python with requests

```python
import requests

BASE_URL = "http://localhost:8000/api/v1"
session = requests.Session()

# Login
response = session.post(f"{BASE_URL}/auth/login", json={
    "username": "admin",
    "password": "yourpassword"
})

# One-shot query
response = session.post(f"{BASE_URL}/query", json={
    "prompt": "Write a hello world function in Python",
    "profile": "claude-code"
})
result = response.json()
print(result["response"])

# Multi-turn conversation
session_id = result["session_id"]

response = session.post(f"{BASE_URL}/conversation", json={
    "prompt": "Now add type hints",
    "session_id": session_id
})
```

### Python with SSE streaming

```python
import requests
import json

BASE_URL = "http://localhost:8000/api/v1"
session = requests.Session()

# Login first...

# Stream query
response = session.post(
    f"{BASE_URL}/conversation/stream",
    json={"prompt": "Write a hello world function"},
    stream=True
)

for line in response.iter_lines():
    if line:
        line = line.decode('utf-8')
        if line.startswith('data: '):
            event = json.loads(line[6:])

            if event['type'] == 'text':
                print(event['content'], end='', flush=True)
            elif event['type'] == 'tool_use':
                print(f"\n[Using tool: {event['name']}]")
            elif event['type'] == 'done':
                print(f"\n\nCompleted. Cost: ${event['metadata']['total_cost_usd']:.4f}")
```

### JavaScript/TypeScript

```typescript
const BASE_URL = 'http://localhost:8000/api/v1';

// Login
await fetch(`${BASE_URL}/auth/login`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  credentials: 'include',
  body: JSON.stringify({ username: 'admin', password: 'yourpassword' })
});

// Query
const response = await fetch(`${BASE_URL}/query`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  credentials: 'include',
  body: JSON.stringify({
    prompt: 'Write a hello world function',
    profile: 'claude-code'
  })
});

const result = await response.json();
console.log(result.response);
```

### JavaScript SSE Streaming

```typescript
async function streamQuery(prompt: string, sessionId?: string) {
  const response = await fetch(`${BASE_URL}/conversation/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ prompt, session_id: sessionId })
  });

  const reader = response.body?.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader!.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const event = JSON.parse(line.slice(6));

        switch (event.type) {
          case 'init':
            console.log('Session:', event.session_id);
            break;
          case 'text':
            process.stdout.write(event.content);
            break;
          case 'tool_use':
            console.log(`\n[Tool: ${event.name}]`);
            break;
          case 'done':
            console.log(`\nCost: $${event.metadata.total_cost_usd}`);
            break;
        }
      }
    }
  }
}
```

### cURL Examples

```bash
# Login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"yourpassword"}' \
  -c cookies.txt

# One-shot query
curl -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -d '{"prompt":"Write hello world in Python","profile":"claude-code"}'

# List sessions
curl http://localhost:8000/api/v1/sessions?limit=10 \
  -b cookies.txt

# Get session with messages
curl http://localhost:8000/api/v1/sessions/sess_abc123 \
  -b cookies.txt
```

---

## Error Responses

All errors follow this format:

```json
{
  "detail": "Error message description"
}
```

### Common HTTP Status Codes

| Code | Description |
|------|-------------|
| 400 | Bad Request - Invalid parameters |
| 401 | Unauthorized - Not logged in or Claude not authenticated |
| 403 | Forbidden - Cannot modify built-in resources |
| 404 | Not Found - Resource doesn't exist |
| 409 | Conflict - Resource already exists |
| 500 | Internal Server Error - Server-side failure |

---

## Security Notes

1. **Session Cookies**: HTTP-only, same-site=lax
2. **Claude Authentication**: Requires running `claude login` in the container
3. **Project Paths**: Validated to prevent directory traversal
4. **Built-in Profiles**: Cannot be modified or deleted
5. **All queries include security instructions** in system prompts

---

## Rate Limits

Currently no rate limiting is enforced. For production deployments, consider adding rate limiting at the reverse proxy level.

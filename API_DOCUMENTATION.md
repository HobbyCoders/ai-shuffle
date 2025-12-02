# AI Hub API Documentation

**Version:** 4.0.0
**Base URL:** `http://localhost:8000`
**API Prefix:** `/api/v1`

---

## Table of Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [Quick Start](#quick-start)
4. [HTTP Endpoints](#http-endpoints)
   - [System](#system)
   - [Authentication](#authentication-endpoints)
   - [Profiles](#profiles)
   - [Projects](#projects)
   - [Sessions](#sessions)
   - [Query & Chat](#query--chat)
   - [API Users](#api-users-admin-only)
   - [Commands & Rewind](#commands--rewind)
5. [WebSocket Endpoints](#websocket-endpoints)
6. [Request/Response Models](#requestresponse-models)
7. [Error Handling](#error-handling)
8. [Rate Limiting](#rate-limiting)
9. [Examples](#examples)

---

## Overview

AI Hub provides a REST API and WebSocket interface for interacting with Claude AI. The API supports:

- **One-shot queries** - Single question/answer interactions
- **Multi-turn conversations** - Persistent chat sessions with context
- **Streaming responses** - Real-time SSE and WebSocket streaming
- **Profile management** - Configure AI behavior with different profiles
- **Project workspaces** - Organize conversations by project
- **Cross-device sync** - Real-time synchronization across devices

---

## Authentication

AI Hub supports two authentication methods:

### 1. Admin Session (Cookie-based)

For web UI users. Login creates an HttpOnly session cookie.

```bash
# Login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your-password"}' \
  -c cookies.txt

# Use the cookie for subsequent requests
curl http://localhost:8000/api/v1/sessions -b cookies.txt
```

### 2. API Key (Bearer Token)

For programmatic access. API keys are created by admins and start with `aih_`.

```bash
# Use API key in Authorization header
curl http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer aih_your_api_key_here"
```

**API Key Features:**
- Can be restricted to specific projects and profiles
- Can be deactivated without deletion
- Usage is tracked per API user
- Keys are hashed in the database (shown only once on creation)

---

## Quick Start

### 1. Check API Status

```bash
curl http://localhost:8000/api/v1/health
```

Response:
```json
{
  "status": "ok",
  "service": "ai-hub",
  "version": "4.0.0",
  "authenticated": false,
  "setup_required": false,
  "claude_authenticated": true
}
```

### 2. Simple Query (One-shot)

```bash
curl -X POST http://localhost:8000/api/v1/query \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "What is the capital of France?",
    "profile": "claude-code"
  }'
```

### 3. Streaming Query

```bash
curl -X POST http://localhost:8000/api/v1/query/stream \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain quantum computing",
    "profile": "claude-code"
  }'
```

### 4. Multi-turn Conversation

```bash
# First message (creates session)
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Help me write a Python function to sort a list",
    "profile": "claude-code"
  }'

# Continue conversation (use returned session_id)
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Now make it handle edge cases",
    "session_id": "returned-session-uuid"
  }'
```

---

## HTTP Endpoints

### System

#### Health Check
```
GET /health
GET /api/v1/health
```
No authentication required.

**Response:**
```json
{
  "status": "ok",
  "service": "ai-hub",
  "version": "4.0.0",
  "authenticated": false,
  "setup_required": false,
  "claude_authenticated": true
}
```

#### Get Version
```
GET /api/v1/version
```
**Response:**
```json
{
  "api_version": "4.0.0",
  "claude_version": "1.0.33"
}
```

#### Get Usage Statistics
```
GET /api/v1/stats
```
Requires authentication.

**Response:**
```json
{
  "total_sessions": 42,
  "total_queries": 156,
  "total_cost_usd": 12.34,
  "total_tokens_in": 50000,
  "total_tokens_out": 100000
}
```

---

### Authentication Endpoints

#### Initial Setup
```
POST /api/v1/auth/setup
```
First-time admin account creation. Only works when no admin exists.

**Request:**
```json
{
  "username": "admin",
  "password": "secure-password"
}
```

#### Admin Login
```
POST /api/v1/auth/login
```
**Request:**
```json
{
  "username": "admin",
  "password": "your-password"
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Login successful",
  "is_admin": true
}
```

#### API Key Web Login
```
POST /api/v1/auth/login/api-key
```
Create a web session using an API key.

**Request:**
```json
{
  "api_key": "aih_your_api_key"
}
```

#### Logout
```
POST /api/v1/auth/logout
```

#### Check Auth Status
```
GET /api/v1/auth/status
```
**Response:**
```json
{
  "authenticated": true,
  "is_admin": true,
  "setup_required": false,
  "claude_authenticated": true,
  "github_authenticated": false,
  "username": "admin",
  "api_user": null
}
```

#### Claude Authentication Status
```
GET /api/v1/auth/claude/status
```
**Response:**
```json
{
  "authenticated": true,
  "auth_method": "OAuth",
  "username": "user@example.com",
  "email": "user@example.com",
  "model_ids": ["claude-sonnet-4-20250514", "claude-opus-4-20250514"]
}
```

---

### Profiles

Profiles define AI behavior, permissions, and tool access.

#### List Profiles
```
GET /api/v1/profiles
```

**Response:**
```json
[
  {
    "id": "claude-code",
    "name": "Claude Code",
    "description": "Full-featured coding assistant",
    "config": {
      "model": "sonnet",
      "permission_mode": "default",
      "allowed_tools": null,
      "disallowed_tools": null
    },
    "is_builtin": true,
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z"
  }
]
```

#### Get Profile
```
GET /api/v1/profiles/{profile_id}
```

#### Create Profile (Admin)
```
POST /api/v1/profiles
```
**Request:**
```json
{
  "id": "safe-assistant",
  "name": "Safe Assistant",
  "description": "Read-only assistant",
  "config": {
    "model": "haiku",
    "permission_mode": "default",
    "allowed_tools": ["Read", "Glob", "Grep"],
    "disallowed_tools": ["Write", "Edit", "Bash"]
  }
}
```

#### Update Profile (Admin)
```
PUT /api/v1/profiles/{profile_id}
```

#### Delete Profile (Admin)
```
DELETE /api/v1/profiles/{profile_id}
```

---

### Projects

Projects organize work into separate workspaces with their own file systems.

#### List Projects
```
GET /api/v1/projects
```

#### Get Project
```
GET /api/v1/projects/{project_id}
```

#### Create Project (Admin)
```
POST /api/v1/projects
```
**Request:**
```json
{
  "id": "my-webapp",
  "name": "My Web App",
  "description": "React frontend project",
  "settings": {
    "default_profile_id": "claude-code",
    "custom_instructions": "Use TypeScript and follow React best practices"
  }
}
```

#### Update Project (Admin)
```
PUT /api/v1/projects/{project_id}
```

#### Delete Project (Admin)
```
DELETE /api/v1/projects/{project_id}
```

#### List Project Files
```
GET /api/v1/projects/{project_id}/files?path=/src
```

#### Upload File
```
POST /api/v1/projects/{project_id}/upload
Content-Type: multipart/form-data
```

---

### Sessions

Sessions store conversation history and metadata.

#### List Sessions
```
GET /api/v1/sessions
```

**Query Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Filter by project |
| `profile_id` | string | Filter by profile |
| `status` | string | Filter by status (active, completed, archived) |
| `api_user_id` | string | Filter by API user |
| `admin_only` | boolean | Only show admin sessions |
| `api_users_only` | boolean | Only show API user sessions |
| `limit` | integer | Max results (default: 50) |
| `offset` | integer | Pagination offset |

#### Get Session with Messages
```
GET /api/v1/sessions/{session_id}
```

**Response:**
```json
{
  "id": "uuid",
  "profile_id": "claude-code",
  "project_id": "my-project",
  "title": "Help with Python",
  "status": "active",
  "total_cost_usd": 0.05,
  "total_tokens_in": 1000,
  "total_tokens_out": 2000,
  "turn_count": 3,
  "created_at": "2024-01-01T12:00:00Z",
  "messages": [
    {
      "id": 1,
      "role": "user",
      "content": "Help me write a function",
      "created_at": "2024-01-01T12:00:00Z"
    },
    {
      "id": 2,
      "role": "assistant",
      "content": "Here's a function...",
      "created_at": "2024-01-01T12:00:01Z"
    }
  ]
}
```

#### Update Session
```
PATCH /api/v1/sessions/{session_id}
```
**Request:**
```json
{
  "title": "New Title",
  "status": "archived"
}
```

#### Delete Session
```
DELETE /api/v1/sessions/{session_id}
```

#### Batch Delete Sessions
```
POST /api/v1/sessions/batch-delete
```
**Request:**
```json
{
  "session_ids": ["uuid1", "uuid2", "uuid3"]
}
```

#### Archive Session
```
POST /api/v1/sessions/{session_id}/archive
```

#### Get Session State (for sync)
```
GET /api/v1/sessions/{session_id}/state
```

---

### Query & Chat

#### One-Shot Query
```
POST /api/v1/query
```
Single question/answer with no conversation history.

**Request:**
```json
{
  "prompt": "What is 2 + 2?",
  "profile": "claude-code",
  "project": "optional-project-id",
  "overrides": {
    "model": "haiku",
    "max_turns": 5,
    "system_prompt_append": "Be concise."
  }
}
```

**Response:**
```json
{
  "response": "2 + 2 equals 4.",
  "session_id": "uuid",
  "metadata": {
    "model": "claude-3-haiku",
    "duration_ms": 1234,
    "total_cost_usd": 0.001,
    "tokens_in": 50,
    "tokens_out": 10,
    "num_turns": 1
  }
}
```

#### Streaming One-Shot Query
```
POST /api/v1/query/stream
```
Returns Server-Sent Events (SSE).

**Event Types:**
```
event: message
data: {"type": "text", "content": "Hello"}

event: message
data: {"type": "tool_use", "name": "Read", "input": {...}, "id": "tool_123"}

event: message
data: {"type": "tool_result", "name": "Read", "output": "..."}

event: message
data: {"type": "done", "session_id": "uuid", "metadata": {...}}
```

#### Multi-Turn Conversation
```
POST /api/v1/conversation
```
Continues an existing session or creates a new one.

**Request:**
```json
{
  "prompt": "Continue from where we left off",
  "session_id": "existing-session-uuid",
  "profile": "claude-code",
  "project": "my-project",
  "device_id": "optional-device-id"
}
```

#### Streaming Conversation
```
POST /api/v1/conversation/stream
```

#### Start Background Conversation
```
POST /api/v1/conversation/start
```
Starts a conversation without waiting for completion. Use WebSocket or polling to get results.

#### Interrupt Session
```
POST /api/v1/session/{session_id}/interrupt
```
Stop an active streaming session.

#### List Active Streams
```
GET /api/v1/streaming/active
```

---

### API Users (Admin Only)

Manage programmatic API access.

#### List API Users
```
GET /api/v1/api-users
```

#### Get API User
```
GET /api/v1/api-users/{user_id}
```

#### Create API User
```
POST /api/v1/api-users
```
**Request:**
```json
{
  "name": "CI Bot",
  "description": "For CI/CD pipeline",
  "project_id": "my-project",
  "profile_id": "claude-code"
}
```

**Response:**
```json
{
  "id": "uuid",
  "name": "CI Bot",
  "description": "For CI/CD pipeline",
  "project_id": "my-project",
  "profile_id": "claude-code",
  "is_active": true,
  "api_key": "aih_xxxxx...",
  "created_at": "2024-01-01T00:00:00Z"
}
```

> **Important:** The `api_key` is only shown once on creation. Store it securely!

#### Update API User
```
PUT /api/v1/api-users/{user_id}
```

#### Regenerate API Key
```
POST /api/v1/api-users/{user_id}/regenerate-key
```
Returns a new API key. The old key is immediately invalidated.

#### Delete API User
```
DELETE /api/v1/api-users/{user_id}
```

---

### Commands & Rewind

#### List Available Commands
```
GET /api/v1/commands/?project_id=optional
```

#### Get Command Details
```
GET /api/v1/commands/{command_name}?project_id=optional
```

#### Get Rewind Checkpoints
```
GET /api/v1/commands/rewind/checkpoints/{session_id}
```

**Response:**
```json
{
  "checkpoints": [
    {
      "uuid": "msg-uuid-1",
      "index": 0,
      "message_preview": "Help me write a function...",
      "full_message": "Help me write a function that...",
      "timestamp": "2024-01-01T12:00:00Z",
      "git_available": true,
      "git_ref": "abc123"
    }
  ],
  "session_id": "session-uuid",
  "current_index": 2
}
```

#### Execute Rewind
```
POST /api/v1/commands/rewind/execute/{session_id}
```
**Request:**
```json
{
  "target_uuid": "msg-uuid-1",
  "restore_chat": true,
  "restore_code": true,
  "include_response": false
}
```

---

## WebSocket Endpoints

### Primary Chat WebSocket

```
WS /api/v1/ws/chat?token=your_api_key
```

The recommended way to interact with AI Hub for real-time streaming.

**Authentication:**
- Query parameter: `?token=aih_your_api_key`
- Or use session cookie

**Client → Server Messages:**

```json
// Send a query
{
  "type": "query",
  "prompt": "Your message here",
  "session_id": null,
  "profile": "claude-code",
  "project": "optional-project-id"
}

// Stop streaming
{
  "type": "stop",
  "session_id": "uuid"
}

// Load session history
{
  "type": "load_session",
  "session_id": "uuid"
}

// Respond to ping
{
  "type": "pong"
}
```

**Server → Client Messages:**

```json
// Query started
{"type": "start", "session_id": "uuid"}

// Text chunk
{"type": "chunk", "content": "Hello, I'll help..."}

// Tool usage
{"type": "tool_use", "name": "Read", "input": {...}, "id": "tool_123"}

// Tool result
{"type": "tool_result", "name": "Read", "output": "file contents..."}

// Query complete
{"type": "done", "session_id": "uuid", "metadata": {...}}

// Session history
{"type": "history", "session_id": "uuid", "session": {...}, "messages": [...]}

// Query interrupted
{"type": "stopped", "session_id": "uuid"}

// Error
{"type": "error", "message": "Error description"}

// Keep-alive
{"type": "ping"}
```

### Session Sync WebSocket

```
WS /api/v1/ws/sessions/{session_id}?device_id=xxx&token=xxx
```

For cross-device synchronization of a specific session.

### Global Sync WebSocket

```
WS /api/v1/ws/global?device_id=xxx&token=xxx
```

For global notifications across all sessions.

---

## Request/Response Models

### QueryRequest

```typescript
interface QueryRequest {
  prompt: string;           // Required: The user's message
  profile?: string;         // Profile ID (default: "claude-code")
  project?: string;         // Project ID
  overrides?: {
    model?: string;         // "sonnet" | "opus" | "haiku"
    system_prompt_append?: string;
    max_turns?: number;
  };
}
```

### ConversationRequest

```typescript
interface ConversationRequest extends QueryRequest {
  session_id?: string;      // Existing session ID (null for new)
  device_id?: string;       // For cross-device sync
}
```

### Session

```typescript
interface Session {
  id: string;
  profile_id: string;
  project_id?: string;
  sdk_session_id?: string;
  title?: string;
  status: "active" | "completed" | "archived";
  total_cost_usd: number;
  total_tokens_in: number;
  total_tokens_out: number;
  turn_count: number;
  created_at: string;
  updated_at: string;
}
```

### SessionMessage

```typescript
interface SessionMessage {
  id: number | string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  type?: "text" | "tool_use" | "tool_result";
  tool_name?: string;
  tool_input?: object;
  metadata?: object;
  created_at?: string;
}
```

### Profile

```typescript
interface Profile {
  id: string;
  name: string;
  description?: string;
  config: {
    model?: "sonnet" | "opus" | "haiku";
    permission_mode?: "default" | "acceptEdits" | "plan" | "bypassPermissions";
    max_turns?: number;
    allowed_tools?: string[];
    disallowed_tools?: string[];
    system_prompt?: object;
    cwd?: string;
    env?: Record<string, string>;
  };
  is_builtin: boolean;
  created_at: string;
  updated_at: string;
}
```

### ApiUser

```typescript
interface ApiUser {
  id: string;
  name: string;
  description?: string;
  project_id?: string;      // Restricts to this project
  profile_id?: string;      // Restricts to this profile
  is_active: boolean;
  created_at: string;
  updated_at: string;
  last_used_at?: string;
}
```

---

## Error Handling

All errors follow a consistent format:

```json
{
  "detail": "Error message describing what went wrong"
}
```

### HTTP Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 201 | Created |
| 204 | No Content (successful deletion) |
| 400 | Bad Request - Invalid input |
| 401 | Unauthorized - Missing or invalid authentication |
| 403 | Forbidden - Insufficient permissions |
| 404 | Not Found - Resource doesn't exist |
| 409 | Conflict - Resource already exists |
| 423 | Locked - Account locked due to failed attempts |
| 429 | Too Many Requests - Rate limited |
| 500 | Internal Server Error |
| 503 | Service Unavailable - Claude not authenticated |

---

## Rate Limiting

### Login Rate Limiting

- **Max failed attempts:** 5 per 15-minute window
- **Lockout duration:** 30 minutes
- **Scope:** Both IP address and username

When locked out, you'll receive:
```json
{
  "detail": "Account locked due to too many failed attempts. Try again in X minutes."
}
```

---

## Examples

### Python Example

```python
import requests

API_URL = "http://localhost:8000/api/v1"
API_KEY = "aih_your_api_key"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# One-shot query
response = requests.post(
    f"{API_URL}/query",
    headers=headers,
    json={
        "prompt": "Write a Python function to calculate factorial",
        "profile": "claude-code"
    }
)
print(response.json()["response"])

# Multi-turn conversation
session_id = None

# First message
response = requests.post(
    f"{API_URL}/conversation",
    headers=headers,
    json={
        "prompt": "Help me build a REST API",
        "profile": "claude-code"
    }
)
result = response.json()
session_id = result["session_id"]
print(result["response"])

# Follow-up
response = requests.post(
    f"{API_URL}/conversation",
    headers=headers,
    json={
        "prompt": "Now add authentication",
        "session_id": session_id
    }
)
print(response.json()["response"])
```

### JavaScript/TypeScript Example

```typescript
const API_URL = "http://localhost:8000/api/v1";
const API_KEY = "aih_your_api_key";

// One-shot query
async function query(prompt: string) {
  const response = await fetch(`${API_URL}/query`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      prompt,
      profile: "claude-code"
    })
  });
  return response.json();
}

// Streaming with EventSource
function streamQuery(prompt: string, onChunk: (text: string) => void) {
  const response = await fetch(`${API_URL}/query/stream`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ prompt, profile: "claude-code" })
  });

  const reader = response.body?.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const text = decoder.decode(value);
    const lines = text.split("\n");

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const data = JSON.parse(line.slice(6));
        if (data.type === "text") {
          onChunk(data.content);
        }
      }
    }
  }
}

// WebSocket chat
function connectChat(onMessage: (msg: any) => void) {
  const ws = new WebSocket(`ws://localhost:8000/api/v1/ws/chat?token=${API_KEY}`);

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    onMessage(data);
  };

  ws.onopen = () => {
    // Send a query
    ws.send(JSON.stringify({
      type: "query",
      prompt: "Hello!",
      session_id: null,
      profile: "claude-code"
    }));
  };

  return ws;
}
```

### cURL Examples

```bash
# Check health
curl http://localhost:8000/api/v1/health

# One-shot query
curl -X POST http://localhost:8000/api/v1/query \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello!", "profile": "claude-code"}'

# List sessions
curl http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer aih_your_api_key"

# Get session with messages
curl http://localhost:8000/api/v1/sessions/SESSION_ID \
  -H "Authorization: Bearer aih_your_api_key"

# Create API user (admin only)
curl -X POST http://localhost:8000/api/v1/api-users \
  -H "Content-Type: application/json" \
  -b cookies.txt \
  -d '{"name": "My Bot", "description": "Automated assistant"}'

# Streaming query (shows SSE events)
curl -N -X POST http://localhost:8000/api/v1/query/stream \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Count to 10", "profile": "claude-code"}'
```

---

## Additional Notes

### Security Best Practices

1. **Store API keys securely** - Never commit them to version control
2. **Use HTTPS in production** - All traffic should be encrypted
3. **Rotate keys regularly** - Use the regenerate endpoint periodically
4. **Restrict API user scope** - Assign specific projects/profiles when possible
5. **Monitor usage** - Check `/api/v1/stats` and session logs regularly

### WebSocket Best Practices

1. **Handle reconnection** - WebSocket connections may drop; implement reconnection logic
2. **Respond to pings** - Send `{"type": "pong"}` when you receive a ping
3. **Use device_id** - For cross-device sync, provide a consistent device identifier
4. **Handle all message types** - Your client should gracefully handle unknown message types

### Performance Tips

1. **Use streaming** - For long responses, streaming provides better UX
2. **Reuse sessions** - Multi-turn conversations maintain context efficiently
3. **Choose appropriate models** - Use `haiku` for simple tasks, `sonnet` for complex ones
4. **Batch operations** - Use batch-delete for multiple sessions

---

*Generated for AI Hub v4.0.0*

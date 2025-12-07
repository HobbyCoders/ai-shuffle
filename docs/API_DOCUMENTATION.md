# AI Hub API Documentation

**Version:** 4.0.0
**Base URL:** `http://localhost:8000`
**API Prefix:** `/api/v1`

---

## Table of Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [Quick Start](#quick-start)
4. [Endpoints](#endpoints)
   - [Health & Status](#health--status)
   - [Query (One-Shot)](#query-one-shot)
   - [Conversations (Multi-Turn)](#conversations-multi-turn)
   - [Sessions](#sessions)
   - [Profiles](#profiles)
   - [Projects](#projects)
5. [WebSocket API](#websocket-api)
6. [Streaming (SSE)](#streaming-sse)
7. [Data Models](#data-models)
8. [Error Handling](#error-handling)
9. [Code Examples](#code-examples)

---

## Overview

AI Hub provides a REST API and WebSocket interface for interacting with Claude AI.

**Key Features:**
- **One-shot queries** - Single question/answer interactions
- **Multi-turn conversations** - Persistent chat sessions with context
- **Streaming responses** - Real-time Server-Sent Events (SSE) and WebSocket streaming
- **Session management** - View and manage your conversation history

---

## Authentication

All API requests require authentication using an API key. Your API key will be provided by your administrator.

**Format:** API keys start with `aih_`

**Usage:** Include your API key in the `Authorization` header:

```
Authorization: Bearer aih_your_api_key_here
```

**Example:**
```bash
curl http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer aih_your_api_key_here"
```

> **Note:** Your API key may be restricted to specific projects and/or profiles. If you receive a 403 error, contact your administrator.

---

## Quick Start

### 1. Verify Your Connection

```bash
curl http://localhost:8000/api/v1/health
```

### 2. Send Your First Query

```bash
curl -X POST http://localhost:8000/api/v1/query \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello! What can you help me with?"}'
```

### 3. Have a Conversation

```bash
# Start a conversation
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Help me write a Python script"}'

# Continue the conversation (use the session_id from the response)
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Add error handling to it",
    "session_id": "uuid-from-previous-response"
  }'
```

---

## Endpoints

### Health & Status

#### Check API Health
```
GET /api/v1/health
```

No authentication required. Use this to verify the API is running.

**Response:**
```json
{
  "status": "ok",
  "service": "ai-hub",
  "version": "4.0.0"
}
```

#### Get Version Info
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

---

### Query (One-Shot)

Use one-shot queries for single question/answer interactions without maintaining conversation history.

#### Send a Query
```
POST /api/v1/query
```

**Request Body:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | Yes | Your message to Claude |
| `project` | string | No | Project ID (if your API key allows multiple projects) |

**Example Request:**
```json
{
  "prompt": "What is the capital of France?"
}
```

**Example Response:**
```json
{
  "response": "The capital of France is Paris.",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "metadata": {
    "model": "claude-sonnet-4-20250514",
    "duration_ms": 1234,
    "total_cost_usd": 0.003,
    "tokens_in": 15,
    "tokens_out": 12,
    "num_turns": 1
  }
}
```

#### Send a Streaming Query
```
POST /api/v1/query/stream
```

Same request body as `/query`, but returns Server-Sent Events. See [Streaming (SSE)](#streaming-sse) for details.

---

### Conversations (Multi-Turn)

Use conversations when you need to maintain context across multiple messages.

#### Send a Message
```
POST /api/v1/conversation
```

**Request Body:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | Yes | Your message to Claude |
| `session_id` | string | No | Session ID to continue (omit to start new) |
| `project` | string | No | Project ID (required for new sessions if your key allows multiple) |

**Start a New Conversation:**
```json
{
  "prompt": "Help me build a REST API in Python"
}
```

**Continue an Existing Conversation:**
```json
{
  "prompt": "Now add authentication",
  "session_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response:**
```json
{
  "response": "I'll help you build a REST API...",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "metadata": {
    "model": "claude-sonnet-4-20250514",
    "duration_ms": 5432,
    "total_cost_usd": 0.015,
    "tokens_in": 250,
    "tokens_out": 800,
    "num_turns": 1
  }
}
```

#### Send a Streaming Message
```
POST /api/v1/conversation/stream
```

Same request body as `/conversation`, but returns Server-Sent Events.

#### Interrupt an Active Session
```
POST /api/v1/session/{session_id}/interrupt
```

Stop a currently streaming response.

**Response:**
```json
{
  "status": "interrupted",
  "session_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

### Sessions

Sessions store your conversation history.

#### List Your Sessions
```
GET /api/v1/sessions
```

**Query Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Filter by project |
| `status` | string | Filter by status: `active`, `completed`, `archived` |
| `limit` | integer | Max results (default: 50) |
| `offset` | integer | Pagination offset |

**Response:**
```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Python REST API",
    "status": "active",
    "total_cost_usd": 0.05,
    "turn_count": 5,
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T11:45:00Z"
  }
]
```

#### Get a Session with Messages
```
GET /api/v1/sessions/{session_id}
```

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Python REST API",
  "status": "active",
  "total_cost_usd": 0.05,
  "total_tokens_in": 1500,
  "total_tokens_out": 3000,
  "turn_count": 5,
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T11:45:00Z",
  "messages": [
    {
      "id": 1,
      "role": "user",
      "content": "Help me build a REST API in Python",
      "created_at": "2024-01-15T10:30:00Z"
    },
    {
      "id": 2,
      "role": "assistant",
      "content": "I'll help you build a REST API...",
      "created_at": "2024-01-15T10:30:05Z"
    }
  ]
}
```

#### Update a Session
```
PATCH /api/v1/sessions/{session_id}
```

**Request Body:**
```json
{
  "title": "My Python API Project"
}
```

#### Delete a Session
```
DELETE /api/v1/sessions/{session_id}
```

Returns `204 No Content` on success.

#### Delete Multiple Sessions
```
POST /api/v1/sessions/batch-delete
```

**Request Body:**
```json
{
  "session_ids": ["uuid1", "uuid2", "uuid3"]
}
```

**Response:**
```json
{
  "deleted_count": 3,
  "total_requested": 3,
  "errors": []
}
```

---

### Profiles

Profiles define how Claude behaves. Your API key may be restricted to a specific profile.

#### List Available Profiles
```
GET /api/v1/profiles
```

**Response:**
```json
[
  {
    "id": "claude-code",
    "name": "Claude Code",
    "description": "Full-featured coding assistant"
  }
]
```

#### Get Profile Details
```
GET /api/v1/profiles/{profile_id}
```

---

### Projects

Projects organize work into separate workspaces. Your API key may be restricted to a specific project.

#### List Available Projects
```
GET /api/v1/projects
```

**Response:**
```json
[
  {
    "id": "my-webapp",
    "name": "My Web App",
    "description": "React frontend project"
  }
]
```

#### Get Project Details
```
GET /api/v1/projects/{project_id}
```

#### List Project Files
```
GET /api/v1/projects/{project_id}/files?path=/src
```

**Response:**
```json
{
  "path": "/src",
  "files": [
    {"name": "index.ts", "type": "file", "size": 1234},
    {"name": "components", "type": "directory"}
  ]
}
```

#### Upload a File
```
POST /api/v1/projects/{project_id}/upload
Content-Type: multipart/form-data
```

**Form Fields:**
- `file`: The file to upload
- `path` (optional): Target directory path

**Response:**
```json
{
  "filename": "script.py",
  "path": "/uploads",
  "full_path": "/uploads/script.py",
  "size": 2048
}
```

---

## WebSocket API

For real-time streaming, connect via WebSocket.

### Connect
```
WS /api/v1/ws/chat?token=aih_your_api_key
```

### Send Messages

**Send a Query:**
```json
{
  "type": "query",
  "prompt": "Your message here",
  "session_id": null,
  "project": "optional-project-id"
}
```

**Continue a Conversation:**
```json
{
  "type": "query",
  "prompt": "Follow-up message",
  "session_id": "existing-session-uuid"
}
```

**Stop Streaming:**
```json
{
  "type": "stop",
  "session_id": "uuid"
}
```

**Load Session History:**
```json
{
  "type": "load_session",
  "session_id": "uuid"
}
```

**Respond to Ping:**
```json
{
  "type": "pong"
}
```

### Receive Messages

| Type | Description | Example |
|------|-------------|---------|
| `start` | Query started | `{"type": "start", "session_id": "uuid"}` |
| `chunk` | Text chunk | `{"type": "chunk", "content": "Hello..."}` |
| `tool_use` | Claude using a tool | `{"type": "tool_use", "name": "Read", "input": {...}}` |
| `tool_result` | Tool result | `{"type": "tool_result", "name": "Read", "output": "..."}` |
| `done` | Query complete | `{"type": "done", "session_id": "uuid", "metadata": {...}}` |
| `stopped` | Query interrupted | `{"type": "stopped", "session_id": "uuid"}` |
| `error` | Error occurred | `{"type": "error", "message": "..."}` |
| `ping` | Keep-alive | `{"type": "ping"}` |
| `history` | Session loaded | `{"type": "history", "session_id": "uuid", "messages": [...]}` |

---

## Streaming (SSE)

The `/query/stream` and `/conversation/stream` endpoints return Server-Sent Events.

### Event Format

```
event: message
data: {"type": "text", "content": "Hello, I'll help you..."}

event: message
data: {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/main.py"}, "id": "tool_123"}

event: message
data: {"type": "tool_result", "name": "Read", "output": "# Main application..."}

event: message
data: {"type": "done", "session_id": "uuid", "metadata": {"total_cost_usd": 0.01}}
```

### Event Types

| Type | Description |
|------|-------------|
| `text` | Text content chunk |
| `tool_use` | Claude is using a tool (includes `name`, `input`, `id`) |
| `tool_result` | Result from a tool (includes `name`, `output`) |
| `done` | Stream complete (includes `session_id`, `metadata`) |
| `error` | Error occurred (includes `message`) |

---

## Data Models

### Session

```typescript
interface Session {
  id: string;
  title: string | null;
  status: "active" | "completed" | "archived";
  total_cost_usd: number;
  total_tokens_in: number;
  total_tokens_out: number;
  turn_count: number;
  created_at: string;  // ISO 8601
  updated_at: string;  // ISO 8601
}
```

### Message

```typescript
interface Message {
  id: number | string;
  role: "user" | "assistant";
  content: string;
  type?: "text" | "tool_use" | "tool_result";
  tool_name?: string;     // For tool_use/tool_result
  tool_input?: object;    // For tool_use
  created_at: string;     // ISO 8601
}
```

### Query Metadata

```typescript
interface QueryMetadata {
  model: string;
  duration_ms: number;
  total_cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  num_turns: number;
}
```

---

## Error Handling

### Error Response Format

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
| 401 | Unauthorized - Missing or invalid API key |
| 403 | Forbidden - API key doesn't have access to this resource |
| 404 | Not Found - Resource doesn't exist |
| 500 | Internal Server Error |
| 503 | Service Unavailable - Claude service not ready |

### Common Errors

**Invalid API Key:**
```json
{
  "detail": "Invalid or missing API key"
}
```

**Access Denied:**
```json
{
  "detail": "API key does not have access to this project"
}
```

**Session Not Found:**
```json
{
  "detail": "Session not found"
}
```

---

## Code Examples

### Python

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
    json={"prompt": "Explain recursion in simple terms"}
)
print(response.json()["response"])

# Multi-turn conversation
session_id = None

# First message
response = requests.post(
    f"{API_URL}/conversation",
    headers=headers,
    json={"prompt": "Help me write a function to calculate fibonacci numbers"}
)
result = response.json()
session_id = result["session_id"]
print(result["response"])

# Follow-up
response = requests.post(
    f"{API_URL}/conversation",
    headers=headers,
    json={
        "prompt": "Now optimize it with memoization",
        "session_id": session_id
    }
)
print(response.json()["response"])
```

### Python with Streaming

```python
import requests

API_URL = "http://localhost:8000/api/v1"
API_KEY = "aih_your_api_key"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

response = requests.post(
    f"{API_URL}/query/stream",
    headers=headers,
    json={"prompt": "Write a haiku about programming"},
    stream=True
)

for line in response.iter_lines():
    if line:
        line = line.decode('utf-8')
        if line.startswith('data: '):
            import json
            data = json.loads(line[6:])
            if data.get('type') == 'text':
                print(data['content'], end='', flush=True)
            elif data.get('type') == 'done':
                print("\n--- Done ---")
```

### JavaScript/TypeScript

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
    body: JSON.stringify({ prompt })
  });
  return response.json();
}

// Multi-turn conversation
async function chat(prompt: string, sessionId?: string) {
  const response = await fetch(`${API_URL}/conversation`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      prompt,
      session_id: sessionId
    })
  });
  return response.json();
}

// Usage
const result = await query("What is TypeScript?");
console.log(result.response);

// Conversation
const msg1 = await chat("Help me write a React component");
const msg2 = await chat("Add props validation", msg1.session_id);
```

### JavaScript with WebSocket

```typescript
const API_KEY = "aih_your_api_key";

const ws = new WebSocket(`ws://localhost:8000/api/v1/ws/chat?token=${API_KEY}`);

ws.onopen = () => {
  console.log("Connected");

  // Send a query
  ws.send(JSON.stringify({
    type: "query",
    prompt: "Hello, Claude!",
    session_id: null
  }));
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case "chunk":
      process.stdout.write(data.content);
      break;
    case "done":
      console.log("\n--- Complete ---");
      console.log("Session ID:", data.session_id);
      break;
    case "error":
      console.error("Error:", data.message);
      break;
    case "ping":
      ws.send(JSON.stringify({ type: "pong" }));
      break;
  }
};

ws.onerror = (error) => {
  console.error("WebSocket error:", error);
};
```

### cURL

```bash
# Check health
curl http://localhost:8000/api/v1/health

# One-shot query
curl -X POST http://localhost:8000/api/v1/query \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is 2 + 2?"}'

# Start a conversation
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Help me write a bash script"}'

# Continue conversation (replace SESSION_ID)
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Add error handling", "session_id": "SESSION_ID"}'

# List your sessions
curl http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer aih_your_api_key"

# Get a specific session
curl http://localhost:8000/api/v1/sessions/SESSION_ID \
  -H "Authorization: Bearer aih_your_api_key"

# Streaming query (shows SSE events)
curl -N -X POST http://localhost:8000/api/v1/query/stream \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Count to 5"}'

# Delete a session
curl -X DELETE http://localhost:8000/api/v1/sessions/SESSION_ID \
  -H "Authorization: Bearer aih_your_api_key"
```

---

## Tips

1. **Use streaming for long responses** - Provides better user experience
2. **Reuse sessions for related tasks** - Maintains context and reduces token usage
3. **Check your session list** - Find previous conversations to continue
4. **Handle errors gracefully** - Always check response status codes
5. **Implement WebSocket reconnection** - Connections may drop; reconnect automatically

---

*AI Hub API v4.0.0*
